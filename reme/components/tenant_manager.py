"""TenantManager — lazy, LRU-bounded lifecycle for per-tenant component sets.

In multi-tenant mode the shared ``Application`` instantiates only workspace-agnostic
components (LLM/embedding clients, tokenizer, service). Everything workspace-bound
(``TENANT_SCOPED_TYPES``) is created here, on demand, one set per active tenant, with a
cap on how many tenant sets stay resident. Inactive tenants cost only disk; a cold
request re-instantiates and re-loads the tenant's persisted index.
"""

import asyncio
import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING

from . import wiring
from .base_component import BaseComponent
from .tenant_context import TenantContext
from ..enumeration import TENANT_SCOPED_TYPES, ComponentEnum
from ..utils import get_logger

if TYPE_CHECKING:
    from .application_context import ApplicationContext


class TenantManager:
    """Owns the resident set of ``TenantContext`` objects and their lifecycle."""

    def __init__(self, global_context: "ApplicationContext") -> None:
        self._global = global_context
        self.config = global_context.app_config.multi_tenant
        self._id_re = re.compile(self.config.tenant_id_pattern)
        self.logger = get_logger()

        # Insertion-ordered LRU: oldest first. Plain dict preserves insertion order;
        # _touch re-inserts to move an entry to the most-recently-used end.
        self._cache: dict[str, TenantContext] = {}
        self._creation_locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

        self._tasks: list[asyncio.Task] = []
        self._stopping = False

    # ----- identity ------------------------------------------------------

    def validate_tenant_id(self, tenant_id: str) -> str:
        """Return the tenant id if it matches the configured pattern; raise otherwise."""
        if not tenant_id or not self._id_re.match(tenant_id):
            raise ValueError(f"Invalid tenant id: {tenant_id!r}")
        return tenant_id

    # ----- acquisition ---------------------------------------------------

    async def get(self, tenant_id: str) -> TenantContext:
        """Return the tenant's context, instantiating and starting it on first use."""
        self.validate_tenant_id(tenant_id)

        cached = self._cache.get(tenant_id)
        if cached is not None:
            self._touch(cached)
            return cached

        lock = await self._creation_lock_for(tenant_id)
        async with lock:
            cached = self._cache.get(tenant_id)  # re-check inside the lock
            if cached is None:
                cached = await self._create(tenant_id)
                self._cache[tenant_id] = cached
            self._touch(cached)

        await self._evict_if_needed()
        return cached

    def _touch(self, tctx: TenantContext) -> None:
        """Mark most-recently-used: move to the end of the LRU and stamp the time."""
        tctx.last_used_at = time.monotonic()
        self._cache.pop(tctx.tenant_id, None)
        self._cache[tctx.tenant_id] = tctx

    async def _creation_lock_for(self, tenant_id: str) -> asyncio.Lock:
        async with self._locks_guard:
            return self._creation_locks.setdefault(tenant_id, asyncio.Lock())

    # ----- construction --------------------------------------------------

    async def _create(self, tenant_id: str) -> TenantContext:
        """Build, wire, and start a tenant's component set."""
        cfg = self._global.app_config
        workspace_dir = str(Path(self.config.workspaces_root).absolute() / tenant_id)
        tenant_config = cfg.model_copy(update={"workspace_dir": workspace_dir})
        wiring.ensure_workspace(workspace_dir, tenant_config)

        tctx = TenantContext(tenant_id, tenant_config, self._global)

        groups: dict[ComponentEnum, dict[str, BaseComponent]] = {}
        for ctype, group in cfg.components.items():
            if ctype not in TENANT_SCOPED_TYPES:
                continue
            for name, comp_cfg in group.items():
                groups.setdefault(ctype, {})[name] = wiring.instantiate(
                    ctype,
                    comp_cfg,
                    tctx,
                    label=f"Tenant '{tenant_id}' component '{name}'",
                    expected_type=BaseComponent,
                    name=name,
                )
        tctx.set_tenant_components(groups)

        # Global components are satisfied externally (e.g. agent_wrapper -> as_llm).
        for comp in wiring.topological_order(groups, external=self._global.components):
            await comp.start()
            tctx.started_components.append(comp)

        self.logger.info(
            f"[tenant_manager] started tenant={tenant_id!r} "
            f"components={sum(len(g) for g in groups.values())} resident={len(self._cache) + 1}",
        )
        return tctx

    # ----- eviction ------------------------------------------------------

    async def _evict_if_needed(self) -> None:
        """Evict least-recently-used, not-in-flight tenants down to the cap."""
        cap = max(1, int(self.config.max_active_tenants))
        while len(self._cache) > cap:
            victim = next((t for t in self._cache.values() if t.in_flight == 0), None)
            if victim is None:
                # Everyone resident is busy; try again after the next release.
                self.logger.warning(
                    f"[tenant_manager] over cap ({len(self._cache)}>{cap}) but all tenants in-flight; deferring evict",
                )
                return
            await self._close_tenant(victim)

    async def _close_tenant(self, tctx: TenantContext) -> None:
        """Persist and close one tenant's components, then drop it from the cache."""
        self._cache.pop(tctx.tenant_id, None)
        for comp in reversed(tctx.started_components):
            try:
                await comp.close()
            except Exception as e:  # pragma: no cover - defensive
                self.logger.exception(f"[tenant_manager] close failed tenant={tctx.tenant_id!r}: {e}")
        tctx.started_components.clear()
        self.logger.info(f"[tenant_manager] evicted tenant={tctx.tenant_id!r} resident={len(self._cache)}")

    async def close_all(self) -> None:
        """Stop maintenance tasks and close every resident tenant (flush to disk)."""
        self._stopping = True
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - shutdown best-effort
                pass
        self._tasks.clear()
        for tctx in list(self._cache.values()):
            await self._close_tenant(tctx)

    # ----- maintenance tasks --------------------------------------------

    def start_maintenance(self) -> None:
        """Launch the idle-eviction and consolidation background loops."""
        loop = asyncio.get_event_loop()
        if self.config.tenant_idle_close_seconds and self.config.idle_sweep_seconds > 0:
            self._tasks.append(loop.create_task(self._idle_sweep_loop()))
        if self.config.consolidation_enabled and self.config.consolidation_interval_seconds > 0:
            self._tasks.append(loop.create_task(self._consolidation_loop()))

    async def _idle_sweep_loop(self) -> None:
        interval = float(self.config.idle_sweep_seconds)
        idle_ttl = float(self.config.tenant_idle_close_seconds)
        while not self._stopping:
            await asyncio.sleep(interval)
            now = time.monotonic()
            for tctx in list(self._cache.values()):
                if tctx.in_flight == 0 and (now - tctx.last_used_at) >= idle_ttl:
                    await self._close_tenant(tctx)

    async def _consolidation_loop(self) -> None:
        """Periodically consolidate (auto_dream) every tenant with new daily content.

        Replaces the disabled ``dream_cron``. This scans the workspaces on DISK — not
        the in-memory LRU — so a tenant is consolidated even if it wrote and then got
        evicted, and the schedule survives restarts. A per-tenant marker file records
        the last consolidation time; a tenant is "due" when any daily file is newer
        than its marker. ``auto_dream``'s own catalog does the fine-grained change
        detection and skips the LLM when nothing actually changed.
        """
        interval = float(self.config.consolidation_interval_seconds)
        # First pass shortly after startup (not a full interval later), so fresh
        # deployments and restarts build digest promptly instead of after ~1 hour.
        await asyncio.sleep(min(60.0, interval))
        while not self._stopping:
            try:
                await self._consolidate_due_tenants()
            except Exception as e:  # pragma: no cover - best-effort background
                self.logger.exception(f"[tenant_manager] consolidation scan failed: {e}")
            await asyncio.sleep(interval)

    async def _consolidate_due_tenants(self) -> None:
        """Run auto_dream for every tenant whose daily content changed since last time."""
        job = self._global.jobs.get(self.config.consolidation_job)
        if job is None:
            self.logger.warning(f"[tenant_manager] consolidation job {self.config.consolidation_job!r} not found")
            return
        root = Path(self.config.workspaces_root).absolute()
        if not root.is_dir():
            return
        due = [tid for tid in self._list_tenant_ids(root) if self._needs_consolidation(root, tid)]
        self.logger.info(f"[tenant_manager] consolidation scan: {len(due)} tenant(s) due")
        if not due:
            return
        semaphore = asyncio.Semaphore(max(1, int(self.config.consolidation_concurrency)))

        async def run_one(tenant_id: str) -> None:
            async with semaphore:
                if self._stopping:
                    return
                try:
                    await job(**{"__tenant__": tenant_id})
                    self._touch_marker(root, tenant_id)  # after the run, so it postdates any file it wrote
                    self.logger.info(f"[tenant_manager] consolidated tenant={tenant_id!r}")
                except Exception as e:  # pragma: no cover - best-effort background
                    self.logger.exception(f"[tenant_manager] consolidation failed tenant={tenant_id!r}: {e}")

        await asyncio.gather(*(run_one(tid) for tid in due))

    def _list_tenant_ids(self, root: Path) -> list[str]:
        """Tenant subdirectories under workspaces_root whose name is a valid tenant id."""
        out: list[str] = []
        try:
            for entry in os.scandir(root):
                if entry.is_dir() and self._id_re.match(entry.name):
                    out.append(entry.name)
        except OSError as e:
            self.logger.error(f"[tenant_manager] scandir failed on {root}: {e}")
        return out

    def _marker_path(self, root: Path, tenant_id: str) -> Path:
        return root / tenant_id / self._global.app_config.metadata_dir / ".dream_scan"

    def _needs_consolidation(self, root: Path, tenant_id: str) -> bool:
        """True if any daily file is newer than the tenant's last-consolidation marker."""
        daily = root / tenant_id / self._global.app_config.daily_dir
        if not daily.is_dir():
            return False
        marker = self._marker_path(root, tenant_id)
        marker_mtime = marker.stat().st_mtime if marker.exists() else 0.0
        for dirpath, _dirs, files in os.walk(daily):
            for name in files:
                try:
                    if os.stat(os.path.join(dirpath, name)).st_mtime > marker_mtime:
                        return True  # early exit: at least one daily file is newer
                except OSError:
                    continue
        return False

    def _touch_marker(self, root: Path, tenant_id: str) -> None:
        marker = self._marker_path(root, tenant_id)
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch()
        except OSError as e:
            self.logger.error(f"[tenant_manager] failed to touch marker for {tenant_id!r}: {e}")

    # ----- introspection -------------------------------------------------

    def active_tenants(self) -> list[str]:
        return list(self._cache.keys())
