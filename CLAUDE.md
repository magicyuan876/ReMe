# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

@AGENTS.md

AGENTS.md (imported above) is the authoritative guide for project principles, the change
workflow, the Step state model, validation commands, and agent guardrails. What follows is
complementary: commands and the big-picture architecture.

## Commands

```bash
pip install -e ".[dev,core]"                                  # setup (Python 3.11+)
pytest tests/unit/test_file.py -v                             # single test file
pytest tests/unit -v --tb=long -s --log-cli-level=WARNING     # main unit suite
pre-commit run --all-files                                    # format + lint (Black/Flake8/Pylint, line length 120)
reme start                                                    # run the service (default 127.0.0.1:2333)
```

CLI arguments use `key=value` with dot notation for nested config overrides, e.g.
`reme start service.port=8181 workspace_dir=/tmp/demo`. Tests under `tests/integration/`
require real credentials (`LLM_API_KEY`, etc.) — do not run them automatically.

## Architecture

ReMe is a config-driven job runner over a file-based memory workspace. Everything the
service can do is declared in YAML (`reme/config/default.yaml`), not hardcoded.

### Request flow

`reme <action> key=value...` (`reme/reme.py`):

- `reme start` builds an `Application` in-process and serves it through the configured
  service backend (`http` FastAPI or `mcp`).
- Any other action becomes a **client call to the already-running server**. The client
  auto-detects the running server's actual backend/transport/host/port by replaying its
  start args; it falls back to local config resolution when no server is found.

On the server, an action name selects a **job** from config; the job builds its **steps**
and runs them sequentially, each step reading/writing a shared `RuntimeContext`, and the
job returns `context.response`.

### Config → registry → instances

`Application` (`reme/application.py`) reads the merged config (built-in YAML + user config
+ dotted CLI overrides + `${ENV_VAR:-default}` interpolation) and instantiates three
groups by looking up each entry's `backend` name in the global registry `R`
(`reme/components/component_registry.py`):

- **Service**: one HTTP or MCP front end. Each job's `parameters` block is a JSON Schema
  that becomes the MCP tool definition / request validation.
- **Components**: named singletons — `as_llm`, `as_embedding`, `embedding_store`,
  `file_store`, `keyword_index` (BM25), `file_graph` (wikilinks), `file_catalog`,
  `file_chunker`, `tokenizer`, `agent_wrapper` (agentscope / claude_code / codex).
  Started in topological dependency order, closed in reverse.
- **Jobs**: named step pipelines. Job backends: `base` (request/response), `stream`,
  `background` (watchfiles directory watchers, e.g. `index_update_loop`,
  `resource_watch_loop`), `cron` (e.g. `dream_cron`).

Registration is import-driven: a class is only discoverable if `R.register(...)` runs,
which requires the module to be reachable via `reme/components/__init__.py` or
`reme/steps/__init__.py` (see AGENTS.md "Change Workflow").

### Steps

Steps (`reme/steps/`, registered under `ComponentEnum.STEP`) are stateless and rebuilt
fresh for every job invocation. They resolve component dependencies lazily via the `Ref`
descriptor in `reme/steps/base_step.py` (kwargs → context → app_context registry).
Step groups: `file_io` (read/write/edit/frontmatter/move), `index` (change watching,
chunking, BM25/vector/hybrid search, wikilink traversal), `evolve` (auto_memory,
auto_resource, dream consolidation — these call LLMs), `common` (version, health, status).

### Memory data flow

The workspace (`session/` → `daily/` → `digest/`, plus `resource/` and rebuildable
`metadata/`) is the source of truth; see README.md for the directory layout. Background
watch jobs keep chunk/BM25/wikilink indexes in sync with the Markdown files; `reme
reindex` rebuilds them from scratch. Search is hybrid: BM25 + optional embeddings, fused
with RRF, then expanded through wikilinks. Embeddings are **disabled by default** —
`as_embedding`/`embedding_store` are commented out in `default.yaml` and
`file_store.default.embedding_store` is `""`.
