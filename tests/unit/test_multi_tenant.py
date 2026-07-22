"""Multi-tenant mode: config, identity resolution, service boundary, and isolation.

The isolation/LRU tests build a real Application from the ``multi_tenant`` config and
drive tenants through the write/search jobs (no network/LLM). They assert that one
tenant's memory is invisible to another, that the LRU evicts and reactivates tenants,
and that per-tenant workspaces are ordinary directories on disk.
"""

import asyncio
import os
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from reme.application import Application
from reme.components.service.http_service import HttpService
from reme.components.service.tenant_resolver import (
    StaticTokenMapResolver,
    TrustedHeaderResolver,
    build_tenant_resolver,
)
from reme.config import resolve_app_config
from reme.enumeration import TENANT_SCOPED_TYPES, ComponentEnum
from reme.schema import ApplicationConfig, MultiTenantAuthConfig, Request


# ---------------------------------------------------------------------------
# Config schema
# ---------------------------------------------------------------------------


def test_multi_tenant_disabled_by_default():
    cfg = ApplicationConfig()
    assert cfg.multi_tenant.enabled is False
    assert cfg.multi_tenant.auth.backend == "trusted_header"
    assert cfg.multi_tenant.auth.header == "X-Reme-Tenant"


def test_tenant_scoped_types_include_workspace_bound_components():
    # The split axis is "references workspace_path / jobs", not "holds state":
    # file_chunker and agent_wrapper are stateless yet workspace-bound.
    for ctype in (
        ComponentEnum.FILE_STORE,
        ComponentEnum.KEYWORD_INDEX,
        ComponentEnum.FILE_GRAPH,
        ComponentEnum.FILE_CATALOG,
        ComponentEnum.EMBEDDING_STORE,
        ComponentEnum.FILE_CHUNKER,
        ComponentEnum.AGENT_WRAPPER,
    ):
        assert ctype in TENANT_SCOPED_TYPES
    # Workspace-agnostic inference clients stay shared.
    assert ComponentEnum.AS_LLM not in TENANT_SCOPED_TYPES
    assert ComponentEnum.TOKENIZER not in TENANT_SCOPED_TYPES


# ---------------------------------------------------------------------------
# Tenant identity resolution
# ---------------------------------------------------------------------------


def test_trusted_header_resolver_case_insensitive():
    r = TrustedHeaderResolver("X-Reme-Tenant")
    assert r.resolve({"X-Reme-Tenant": "alice"}) == "alice"
    assert r.resolve({"x-reme-tenant": "bob"}) == "bob"
    assert r.resolve({"other": "x"}) is None
    assert r.resolve({"X-Reme-Tenant": "   "}) is None


def test_static_token_map_resolver_bearer():
    r = StaticTokenMapResolver("Authorization", {"tok-a": "alice", "tok-b": "bob"})
    assert r.resolve({"Authorization": "Bearer tok-a"}) == "alice"
    assert r.resolve({"authorization": "tok-b"}) == "bob"
    assert r.resolve({"Authorization": "Bearer nope"}) is None
    assert r.resolve({}) is None


def test_build_tenant_resolver_selects_backend():
    assert isinstance(build_tenant_resolver(MultiTenantAuthConfig(backend="trusted_header")), TrustedHeaderResolver)
    assert isinstance(
        build_tenant_resolver(MultiTenantAuthConfig(backend="static_token_map")),
        StaticTokenMapResolver,
    )
    with pytest.raises(ValueError):
        build_tenant_resolver(MultiTenantAuthConfig(backend="nope"))


# ---------------------------------------------------------------------------
# HTTP service boundary
# ---------------------------------------------------------------------------


def _http_service(enabled: bool, exempt=("version",)):
    svc = HttpService()
    svc._mt_enabled = enabled
    svc._resolver = TrustedHeaderResolver("X-Reme-Tenant") if enabled else None
    svc._exempt_jobs = set(exempt)
    return svc


def _req(**fields):
    return Request(**fields)


def _http_request(headers):
    return SimpleNamespace(headers=headers)


def test_boundary_passthrough_when_disabled():
    svc = _http_service(enabled=False)
    job = SimpleNamespace(name="search")
    out = svc._boundary_kwargs(job, _req(query="hi"), _http_request({}))
    assert out == {"query": "hi"}
    assert "__tenant__" not in out


def test_boundary_injects_tenant_from_header():
    svc = _http_service(enabled=True)
    job = SimpleNamespace(name="search")
    out = svc._boundary_kwargs(job, _req(query="hi"), _http_request({"X-Reme-Tenant": "alice"}))
    assert out["__tenant__"] == "alice"
    assert out["query"] == "hi"


def test_boundary_rejects_body_tenant_fields():
    svc = _http_service(enabled=True)
    job = SimpleNamespace(name="search")
    with pytest.raises(HTTPException) as ei:
        svc._boundary_kwargs(job, _req(query="hi", tenant_id="bob"), _http_request({"X-Reme-Tenant": "alice"}))
    assert ei.value.status_code == 400


def test_boundary_401_without_credentials():
    svc = _http_service(enabled=True)
    job = SimpleNamespace(name="search")
    with pytest.raises(HTTPException) as ei:
        svc._boundary_kwargs(job, _req(query="hi"), _http_request({}))
    assert ei.value.status_code == 401


def test_boundary_exempt_ops_job_needs_no_tenant():
    svc = _http_service(enabled=True, exempt=("version",))
    job = SimpleNamespace(name="version")
    out = svc._boundary_kwargs(job, _req(), _http_request({}))
    assert "__tenant__" not in out


# ---------------------------------------------------------------------------
# End-to-end isolation, LRU, reactivation (real Application, no network)
# ---------------------------------------------------------------------------


def _build_mt_app(workspaces_root, max_active=300):
    os.environ.setdefault("LLM_API_KEY", "dummy")
    os.environ.setdefault("LLM_BASE_URL", "http://localhost:9/none")
    cfg = resolve_app_config(
        log_config=False,
        config="multi_tenant",
        multi_tenant={"workspaces_root": str(workspaces_root), "max_active_tenants": max_active},
        enable_logo=False,
        log_to_console=False,
        log_to_file=False,
    )
    return Application(**cfg)


def test_tenant_write_search_isolation(tmp_path):
    async def run():
        app = _build_mt_app(tmp_path / "tenants")
        await app.start()
        try:
            await app.run_job(
                "write", __tenant__="alice", path="digest/wiki/coffee.md",
                name="Coffee", description="p", content="# Coffee\nAlice loves espresso.",
            )
            await app.run_job(
                "write", __tenant__="bob", path="digest/wiki/tea.md",
                name="Tea", description="p", content="# Tea\nBob prefers oolong.",
            )

            alice = await app.run_job("search", __tenant__="alice", query="espresso coffee", limit=5)
            bob = await app.run_job("search", __tenant__="bob", query="espresso coffee", limit=5)
            assert "espresso" in (alice.answer or "").lower()
            assert "espresso" not in (bob.answer or "").lower()  # no cross-tenant leak

            # Per-tenant workspaces are ordinary directories.
            assert (tmp_path / "tenants" / "alice" / "digest" / "wiki" / "coffee.md").exists()
            assert (tmp_path / "tenants" / "bob" / "digest" / "wiki" / "tea.md").exists()
        finally:
            await app.close()

    asyncio.run(run())


def test_lru_evicts_and_reactivates(tmp_path):
    async def run():
        app = _build_mt_app(tmp_path / "tenants", max_active=2)
        await app.start()
        tm = app.context.tenant_manager
        try:
            for name, word in (("alice", "espresso"), ("bob", "oolong"), ("carol", "matcha")):
                await app.run_job(
                    "write", __tenant__=name, path="digest/wiki/note.md",
                    name="N", description="p", content=f"# N\n{name} likes {word}.",
                )
            # Cap is 2, so at most 2 tenants remain resident.
            assert len(tm.active_tenants()) <= 2
            # A tenant evicted from memory reloads its persisted index from disk.
            reloaded = await app.run_job("search", __tenant__="alice", query="espresso", limit=5)
            assert "espresso" in (reloaded.answer or "").lower()
        finally:
            await app.close()

    asyncio.run(run())


def test_missing_tenant_manager_raises_on_tenant_call(tmp_path):
    # A single-tenant Application must reject a __tenant__ kwarg (no MT wiring).
    async def run():
        cfg = resolve_app_config(
            log_config=False,
            workspace_dir=str(tmp_path / "ws"),
            enable_logo=False,
            log_to_console=False,
            log_to_file=False,
        )
        app = Application(**cfg)
        await app.start()
        try:
            with pytest.raises(RuntimeError, match="multi-tenant mode is not active"):
                await app.run_job("search", __tenant__="alice", query="x", limit=1)
        finally:
            await app.close()

    asyncio.run(run())


def test_multi_tenant_rejects_background_jobs(tmp_path):
    # The default config has watch/cron BackgroundJobs; enabling MT on it must fail fast.
    cfg = resolve_app_config(
        log_config=False,
        config="default",
        multi_tenant={"enabled": True, "workspaces_root": str(tmp_path / "t")},
        enable_logo=False,
        log_to_console=False,
        log_to_file=False,
    )
    with pytest.raises(ValueError, match="background/cron"):
        Application(**cfg)
