# Proposal: Native Multi-Tenant Workspaces

- Status: Draft for discussion
- Related issue: [#368](https://github.com/agentscope-ai/ReMe/issues/368)
- Scope: `reme` service layer, application wiring, and component lifecycle. No change to the on-disk workspace layout or the memory file format.

## 1. Summary

ReMe currently binds one `Application` process to exactly one workspace. Callers who serve
many end users (the scenario raised in issue #368) must either run one ReMe process per
user, which multiplies the fixed process cost by the number of users, or share one
workspace across users, which mixes their memories in both the retrieval path and the
consolidation pipeline.

This proposal makes the tenant a first-class request dimension **inside** ReMe while
keeping every tenant's memory a plain, self-contained workspace directory:

- One service process hosts many tenants.
- Each tenant maps to `workspaces_root/<tenant_id>/`, an ordinary ReMe workspace that the
  owner can still read, edit, back up, and take away as files.
- Stateful data components are instantiated per tenant, lazily, with an LRU bound on
  resident tenants. Stateless components are shared.
- The tenant identity is resolved at the service boundary and injected into the request
  context; it is never accepted from the request payload.

The design deliberately does **not** introduce a shared data plane (one store holding all
tenants' chunks). Section 4 explains why that variant conflicts with ReMe's storage model.

## 2. Motivation

Issue #368 asks how to scope recall to the current user. The current answers are:

| Option | Problem |
| --- | --- |
| One ReMe process per user | Fixed cost (interpreter + imports, a port, an event loop) is paid per user; untenable beyond tens of users. |
| Shared workspace + frontmatter tag filtering | Covers only the `search` read path. `read`/`list`/`traverse` remain unscoped, link expansion surfaces neighbors without a filter, and the write pipeline (`auto_memory` → daily → `auto_dream` → digest) merges facts from different users into shared daily notes, digest nodes, and `interests.yaml`. |
| Embedding the SDK and managing N `Application` objects in a host process | Works today (the QwenPaw integration path), but every adopter has to rebuild the same routing, lifecycle, and lifecycle-safety layer outside ReMe, without a stated compatibility contract. |

Deployments with thousands of end users need the third option's economics with
first-party support: single service, tenant in the protocol, per-tenant isolation across
*all* jobs including the consolidation pipeline.

## 3. Goals and non-goals

Goals:

1. One ReMe service process serves N tenants with per-tenant isolation for every job
   (retrieval, file I/O, frontmatter, and the auto_memory/auto_dream pipeline).
2. Preserve the file-native contract: each tenant's memory is a normal workspace
   directory; indexes and caches stay rebuildable per tenant; `reme reindex` semantics are
   unchanged within a tenant.
3. Bounded memory: resident tenant state is capped and evictable; inactive tenants cost
   only disk.
4. Full backward compatibility: single-workspace deployments behave exactly as today, and
   multi-tenant mode is opt-in.

Non-goals:

1. A shared retrieval index across tenants (see section 4).
2. Billing, quotas, and per-tenant credential management. These are host concerns; the
   design only leaves room for them.
3. Cross-tenant search or sharing. Out of scope for this proposal.

## 4. Why not a shared data plane

The textbook multi-tenant design — one store, every chunk tagged with a tenant id,
filters at query time — fits engines like the Elasticsearch/ChromaDB backends of ReMe
0.2.x, but conflicts with the v4 storage model in three places:

1. **Persistence and memory.** `LocalFileStore` loads its full chunk set from a zstd JSONL
   snapshot at start (`reme/components/file_store/local_file_store.py`). A single shared
   store would load every tenant's chunks at boot, making resident memory proportional to
   registered tenants rather than active tenants. Splitting the snapshot into per-tenant
   segments with lazy loading re-creates per-tenant stores in all but name.
2. **BM25 statistics.** A shared keyword index mixes document-frequency statistics across
   tenants: one tenant's vocabulary distorts another tenant's scores. This is a retrieval
   *quality* defect, not only a filtering cost. Per-tenant statistics imply per-tenant
   index instances.
3. **Wikilink graph.** `[[link]]` resolution and traversal must stay inside a tenant.
   A tenant-partitioned graph is equivalent to per-tenant `file_graph` instances.

Conclusion: under a file-native, memory-resident-index design, the natural unit of tenancy
is *a directory plus a set of stateful component instances*. The proposal therefore keeps
per-tenant component sets and moves their management into the kernel, where it can be
correct once, instead of being rebuilt by every host application.

## 5. Design

### 5.1 Tenant model

```
<workspaces_root>/
├── <tenant_id>/            # a normal ReMe workspace, unchanged layout
│   ├── metadata/
│   ├── session/
│   ├── resource/
│   ├── daily/
│   └── digest/
└── ...
```

`tenant_id` is an opaque, validated identifier (safe charset, no path separators). Every
existing guarantee about a workspace — user-owned files as the source of truth,
rebuildable indexes, portability — holds per directory. Migrating a tenant in or out of a
multi-tenant deployment is a directory move.

### 5.2 Component classification

Components split into two groups:

- **Shared, stateless with regard to workspace data**: `as_llm`, `tokenizer`,
  `file_chunker`, `agent_wrapper`. One instance each, as today.
- **Tenant-scoped, stateful**: `file_store`, `keyword_index`, `file_graph`,
  `file_catalog` (all named instances), `embedding_store`. Instantiated per active
  tenant from the same component configs used today.

This classification is the main reason the change is tractable: only the second group
needs lifecycle management, and its members are already workspace-scoped objects whose
persistence lives under `<workspace>/metadata/`.

### 5.3 TenantManager

A new object owned by `ApplicationContext`:

- `get(tenant_id) -> TenantContext`: returns the tenant's component set, instantiating
  and `start()`-ing it on first use (cold start = loading the tenant's zstd snapshots;
  sub-second at personal scale).
- LRU eviction: `max_active_tenants` bounds resident tenants; eviction awaits in-flight
  jobs for that tenant, then `close()`s the set (which persists chunk stores exactly as a
  normal shutdown does). `tenant_idle_close_seconds` closes idle tenants early.
- Concurrency: per-tenant instantiation is serialized; jobs for different tenants run
  concurrently on the shared event loop as they do today.

`TenantContext` holds the per-tenant component instances plus a per-tenant slice of what
is currently global mutable state in `ApplicationContext.metadata` (for example
`tool_contexts`), so cross-invocation state cannot leak between tenants.

### 5.4 Request flow and identity

- The tenant is resolved at the service boundary — an authentication hook on the HTTP and
  MCP services — and injected into the job invocation. It is **rejected** when supplied in
  the request payload, generalizing the existing `injected_job_kwargs` conflict rule in
  `MCPService.add_job` (`reme/components/service/mcp_service.py`) from per-server-static
  to per-request.
- `RuntimeContext` carries the resolved `TenantContext`. `BaseJob.__call__` performs the
  resolution once before building steps.
- The authentication hook itself is pluggable (map API key / bearer token / mTLS identity
  to `tenant_id`); shipping a trivial static-token map is enough for the first iteration.

### 5.5 Implementation chokepoints

Two existing indirection points make the data-plane change small:

1. **Path resolution.** Every component and step derives file paths from the single
   `BaseComponent.workspace_path` property
   (`reme/components/base_component.py`), which reads
   `app_context.app_config.workspace_dir`. In multi-tenant mode it returns the workspace
   of the tenant bound to the current invocation. All file I/O, chunker paths, and
   metadata/persistence paths follow from this one property.
2. **Component resolution.** Steps acquire components exclusively through the `Ref`
   descriptor's three-level fallback (kwargs → context → application registry) in
   `reme/steps/base_step.py`. For tenant-scoped component types, the third level consults
   the invocation's `TenantContext` instead of the global registry. Shared component
   types are unchanged.

Job code, step code, schemas, and the workspace file format do not change.

### 5.6 Jobs in multi-tenant mode

- **Watch loops** (`index_update_loop`, `resource_watch_loop`, `digest_watch_loop`) are
  disabled: N tenants cannot each hold OS file watchers, and in a service deployment all
  writes arrive through the API anyway. The write path instead triggers an explicit index
  update: `update_index_step` already consumes a `changes` batch from the request context
  (`reme/steps/index/update_changes.py`), the same pattern the public `auto_resource` job
  uses, so a `write`-then-index composition is a config-level change.
- **`dream_cron`** becomes a per-tenant queued job: enqueue only tenants with new daily
  notes for the day, with a global concurrency cap. This both bounds LLM spend and avoids
  waking every tenant at the same wall-clock minute.
- All request/response jobs (`search`, `read`, `write`, `edit`, `traverse`,
  `auto_memory`, `auto_dream`, `proactive`, …) work unchanged against the tenant's
  component set — including link expansion, which is naturally scoped because the graph
  itself is tenant-scoped.

### 5.7 Configuration surface

```yaml
service:
  backend: http
  multi_tenant:
    enabled: true            # default false — single-workspace mode is untouched
    workspaces_root: /var/lib/reme/workspaces
    max_active_tenants: 300
    tenant_idle_close_seconds: 1800
    auth: static_token_map   # pluggable resolver: credential -> tenant_id
```

With `enabled: false` (the default), nothing changes: one workspace, watch loops on,
identical behavior to today.

## 6. Backward compatibility and migration

- Single-tenant mode is the default and its code path is untouched.
- A tenant workspace is byte-identical to a single-user workspace. Migration in either
  direction is `mv` plus `reme reindex`.
- Public schemas gain no required fields; tenant identity lives in the service boundary,
  not in job parameter schemas.

## 7. Capacity expectations

Assuming personal-scale tenants (thousands of markdown files, embeddings disabled):

- Resident cost per active tenant: roughly tens of MB (chunk text + BM25 + graph;
  measurable per deployment via the `status` job).
- 5,000 registered tenants with ~5 % concurrently resident ≈ 250 active tenants ≈
  single-digit GB per shard process. Sharding by `hash(tenant_id)` across a few processes
  scales this horizontally with no cross-process coordination, because a tenant's
  directory is owned by exactly one shard.
- Inactive tenants cost disk only.

## 8. Phased plan

1. **Phase 0 — embedded contract.** Document and test that multiple `Application`
   instances may coexist in one process (the QwenPaw embedding path already relies on a
   single instance; the registry is read-only after import, and per-instance state lives
   in `ApplicationContext`). This unblocks host-layer routing immediately and is useful
   regardless of the rest of the proposal.
2. **Phase 1 — kernel tenancy.** `TenantManager`, `TenantContext`, the two chokepoint
   changes (5.5), service-boundary identity injection (5.4), multi-tenant config (5.7),
   and the watch-loop/dream adjustments (5.6).
3. **Phase 2 — operations.** Per-tenant scheduling fairness for LLM-heavy jobs, quota
   hooks, richer auth resolvers, and per-tenant `status` reporting.

## 9. Alternatives considered

- **Frontmatter tag filtering** (the direction sketched in #368): useful as a lightweight
  *cooperative* filter, but it scopes only `search`, leaves `read`/`list`/`traverse` and
  link expansion unscoped, and does not give the consolidation pipeline a user dimension;
  memories from different users still merge into shared daily/digest files. It also
  requires containment semantics that `_matches_search_filter` /
  `_value_matches` (`reme/components/file_store/local_file_store.py`) do not have today.
- **One process per tenant**: correct isolation, prohibitive fixed cost at scale.
- **Host-layer router over N embedded Applications**: economically equivalent to this
  proposal and possible today, but every adopter re-implements routing, LRU lifecycle,
  identity, and write-path indexing without a compatibility contract. This proposal is
  that same architecture moved into the kernel, where it is built once.
- **Shared data plane with tenant-tagged chunks**: rejected for the reasons in section 4.

## 10. Security considerations

- Tenant identity is asserted by the service boundary, never by the caller payload.
- All path handling already normalizes to workspace-relative form; per-tenant
  `workspace_path` confines every job, including `move`/`delete`, to the tenant
  directory.
- Per-tenant `TenantContext` state removes cross-tenant leakage through shared in-memory
  buckets such as `tool_contexts`.
- Watch-loop removal in multi-tenant mode shrinks the attack surface to the API.

## 11. Open questions

1. Should `tenant_idle_close_seconds` eviction persist BM25 state incrementally, or is
   close-time persistence (current behavior) sufficient?
2. Does `dream` scheduling belong in the kernel (a queue job type) or remain a host
   concern in Phase 2?
3. Should Phase 0's multi-instance guarantee become part of the public SDK contract in
   `docs/`?
