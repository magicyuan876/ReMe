"""TenantContext — an ApplicationContext-shaped view scoped to one tenant.

Components and steps never hold a workspace path or a sibling component directly;
they derive everything from ``self.app_context`` (paths via ``workspace_path`` →
``app_config.workspace_dir``; dependencies via ``app_context.components``; cross-call
state via ``app_context.metadata``; nested jobs via ``app_context.jobs``). So making a
tenant "just work" is a matter of handing its components and steps a context object with
the same shape as ``ApplicationContext`` but pointed at the tenant's workspace, component
set, and state bucket. This class is that object.

It is deliberately duck-typed rather than a subclass of ``ApplicationContext`` (whose
``__init__`` parses config and owns process-wide wiring).
"""

import asyncio
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from ..enumeration import TENANT_SCOPED_TYPES, ComponentEnum

if TYPE_CHECKING:
    from .application_context import ApplicationContext
    from .base_component import BaseComponent
    from .job import BaseJob


class _ChainedComponents(Mapping):
    """Per-enum component lookup: tenant-scoped types resolve to the tenant's own
    instances, everything else falls through to the shared process-wide set.

    Supports both ``components[enum]`` (Ref resolution) and
    ``components.get(enum, {})`` (bind resolution)."""

    def __init__(
        self,
        tenant_groups: dict[ComponentEnum, dict[str, "BaseComponent"]],
        global_components: Mapping[ComponentEnum, dict[str, "BaseComponent"]],
    ) -> None:
        self._tenant = tenant_groups
        self._global = global_components

    def _group(self, ctype: ComponentEnum) -> dict[str, "BaseComponent"]:
        if ctype in TENANT_SCOPED_TYPES:
            return self._tenant.get(ctype, {})
        return self._global.get(ctype, {})

    def __getitem__(self, ctype: ComponentEnum) -> dict[str, "BaseComponent"]:
        return self._group(ctype)

    def get(self, ctype: ComponentEnum, default: Any = None) -> Any:  # noqa: A003
        group = self._group(ctype)
        return group if group else (default if default is not None else group)

    def __iter__(self):
        seen = set(self._tenant) | set(self._global)
        return iter(seen)

    def __len__(self) -> int:
        return len(set(self._tenant) | set(self._global))


class _BoundJob:
    """Wraps a shared job so a nested ``run_job`` from inside tenant execution keeps
    the tenant binding: the tenant id is re-injected as the reserved ``__tenant__``
    kwarg, which the target job's ``__call__`` resolves back into a TenantContext."""

    __slots__ = ("_job", "_tenant_id")

    def __init__(self, job: "BaseJob", tenant_id: str) -> None:
        self._job = job
        self._tenant_id = tenant_id

    def __getattr__(self, item: str) -> Any:
        return getattr(self._job, item)

    async def __call__(self, **kwargs) -> Any:
        kwargs.setdefault("__tenant__", self._tenant_id)
        return await self._job(**kwargs)


class _TenantJobs(Mapping):
    """Job registry view returning tenant-bound job wrappers."""

    def __init__(self, global_jobs: Mapping[str, "BaseJob"], tenant_id: str) -> None:
        self._jobs = global_jobs
        self._tenant_id = tenant_id

    def __getitem__(self, name: str) -> _BoundJob:
        return _BoundJob(self._jobs[name], self._tenant_id)

    def get(self, name: str, default: Any = None) -> Any:
        job = self._jobs.get(name)
        return _BoundJob(job, self._tenant_id) if job is not None else default

    def __contains__(self, name: object) -> bool:
        return name in self._jobs

    def __iter__(self):
        return iter(self._jobs)

    def __len__(self) -> int:
        return len(self._jobs)


class TenantContext:
    """An ApplicationContext-shaped runtime context bound to a single tenant."""

    def __init__(
        self,
        tenant_id: str,
        app_config,
        global_context: "ApplicationContext",
    ) -> None:
        self.tenant_id = tenant_id
        # Config copy with workspace_dir already rewritten to the tenant's workspace.
        self.app_config = app_config
        self._global = global_context

        # Filled by TenantManager after it instantiates the tenant's components
        # (resolves the TenantContext <-> component construction cycle).
        self._tenant_components: dict[ComponentEnum, dict[str, "BaseComponent"]] = {}
        self.components = _ChainedComponents(self._tenant_components, global_context.components)
        self.metadata: dict[str, Any] = {}
        self.jobs = _TenantJobs(global_context.jobs, tenant_id)

        # Lifecycle bookkeeping owned by TenantManager.
        self.last_used_at: float = 0.0
        self.in_flight: int = 0
        self.lock: asyncio.Lock = asyncio.Lock()
        self.started_components: list["BaseComponent"] = []

    # ----- passthrough to the shared process-wide context ----------------

    @property
    def service(self):
        return self._global.service

    @property
    def thread_pool(self):
        return self._global.thread_pool

    @property
    def tenant_manager(self):
        return getattr(self._global, "tenant_manager", None)

    # ----- tenant component set ------------------------------------------

    def set_tenant_components(self, components: dict[ComponentEnum, dict[str, "BaseComponent"]]) -> None:
        """Install the tenant's instantiated component groups (called once by TenantManager)."""
        self._tenant_components.clear()
        self._tenant_components.update(components)
