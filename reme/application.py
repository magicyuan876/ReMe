"""Main application entry point."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import AsyncGenerator, TypeVar

from . import __version__
from .components import BaseComponent, ApplicationContext, wiring
from .components.job import BackgroundJob, BaseJob, CronJob, StreamJob
from .components.service import BaseService
from .components.tenant_manager import TenantManager
from .enumeration import ComponentEnum, TENANT_SCOPED_TYPES
from .schema import ComponentConfig, Response, StreamChunk
from .utils import execute_stream_task, print_logo, get_logger

T = TypeVar("T", bound=BaseComponent)


class Application(BaseComponent):
    """Wires components from config and runs jobs against them."""

    def __init__(self, **kwargs) -> None:
        self.context = ApplicationContext(**kwargs)
        self._started_components: list[BaseComponent] = []

        self._setup_workspace_directories()

        if self.config.enable_logo:
            print_logo(self.config)
        logger = get_logger(
            log_to_console=self.config.log_to_console,
            log_to_file=self.config.log_to_file,
            force_init=True,
        )
        logger.info(f"Initializing {self.config.app_name} Application v{__version__}")
        super().__init__()

        self._init_service()
        self._init_components()
        self._init_jobs()
        self._init_multi_tenant()

    @property
    def config(self):
        """Typed view onto the application config held by the context."""
        return self.context.app_config

    # ----- Wiring (called once during __init__) --------------------------

    def _setup_workspace_directories(self) -> None:
        """Ensure the workspace root and configured subdirectories exist on disk.

        In multi-tenant mode only the tenants root is created here; each tenant's
        workspace subdirectories are created lazily by the TenantManager on first use.
        """
        cfg = self.config
        if cfg.multi_tenant.enabled:
            root = cfg.multi_tenant.workspaces_root
            if not root:
                raise ValueError("multi_tenant.enabled requires multi_tenant.workspaces_root")
            Path(root).absolute().mkdir(parents=True, exist_ok=True)
            return
        wiring.ensure_workspace(cfg.workspace_dir, cfg)

    def _init_service(self) -> None:
        """Instantiate the single service backend declared in config.service."""
        self.context.service = self._instantiate(
            ComponentEnum.SERVICE,
            self.config.service,
            label="Service",
            expected_type=BaseService,
        )

    def _init_components(self) -> None:
        """Instantiate every component declared under config.components.

        In multi-tenant mode, workspace-bound component types (TENANT_SCOPED_TYPES) are
        NOT instantiated here — their configs stay as templates that the TenantManager
        instantiates per tenant. Only workspace-agnostic shared components are built now.
        """
        multi_tenant = self.config.multi_tenant.enabled
        for ctype, group in self.config.components.items():
            self.context.components[ctype] = {}
            if multi_tenant and ctype in TENANT_SCOPED_TYPES:
                continue
            for name, cfg in group.items():
                self.context.components[ctype][name] = self._instantiate(
                    ctype,
                    cfg,
                    label=f"Component '{name}'",
                    expected_type=BaseComponent,
                    name=name,
                )

    def _init_jobs(self) -> None:
        """Instantiate every job declared under config.jobs."""
        for name, cfg in self.config.jobs.items():
            self.context.jobs[name] = self._instantiate(
                ComponentEnum.JOB,
                cfg,
                label=f"Job '{name}'",
                expected_type=BaseJob,
                name=name,
            )

    def _instantiate(
        self,
        ctype: ComponentEnum,
        cfg: ComponentConfig,
        *,
        label: str,
        expected_type: type[T],
        name: str | None = None,
    ) -> T:
        """Construct a component bound to this Application's shared context."""
        return wiring.instantiate(ctype, cfg, self.context, label=label, expected_type=expected_type, name=name)

    def _topological_order(self) -> list[BaseComponent]:
        """Return the shared components in dependency order (delegates to wiring)."""
        return wiring.topological_order(self.context.components)

    def _init_multi_tenant(self) -> None:
        """In multi-tenant mode, validate job compatibility and build the TenantManager."""
        if not self.config.multi_tenant.enabled:
            return
        background = sorted(name for name, job in self.context.jobs.items() if isinstance(job, BackgroundJob))
        if background:
            raise ValueError(
                "multi_tenant mode does not support background/cron jobs "
                f"({', '.join(background)}); use the multi_tenant config template "
                "(watch loops and dream_cron removed, consolidation runs per-tenant).",
            )
        self.context.tenant_manager = TenantManager(self.context)
        self.logger.info(
            f"Multi-tenant mode enabled: workspaces_root={self.config.multi_tenant.workspaces_root!r} "
            f"max_active_tenants={self.config.multi_tenant.max_active_tenants}",
        )

    # ----- Lifecycle -----------------------------------------------------

    async def _start(self) -> None:
        """Start components, then jobs as base > stream > background > cron."""
        pool_size = self.config.thread_pool_max_workers
        if pool_size > 0:
            self.context.thread_pool = ThreadPoolExecutor(max_workers=pool_size)
            self.logger.info(f"Thread pool created with max_workers={pool_size}")
        try:
            components = self._topological_order()
            jobs = list(self.context.jobs.values())
            base_jobs = [j for j in jobs if not isinstance(j, (StreamJob, BackgroundJob))]
            stream_jobs = [j for j in jobs if isinstance(j, StreamJob)]
            background_jobs = [j for j in jobs if isinstance(j, BackgroundJob) and not isinstance(j, CronJob)]
            cron_jobs = [j for j in jobs if isinstance(j, CronJob)]
            for c in components + base_jobs + stream_jobs + background_jobs + cron_jobs:
                await self._start_one(c)
            if getattr(self.context, "tenant_manager", None) is not None:
                self.context.tenant_manager.start_maintenance()
        except Exception:
            await self._close()
            raise

    async def _start_one(self, c: BaseComponent) -> None:
        """Start one component and record it for ordered shutdown."""
        try:
            if isinstance(c, BackgroundJob):
                self.logger.info(f"Starting background job: {c.name}")
            await c.start()
            self._started_components.append(c)
        except Exception as e:
            self.logger.exception(f"Failed to start {c.component_type.value}:{c.name}: {e}")
            raise

    async def _close(self) -> None:
        """Close in reverse start order so every peer outlives its dependents."""
        # Close tenant component sets first: they depend on shared components
        # (e.g. a tenant embedding_store on the shared as_embedding).
        if getattr(self.context, "tenant_manager", None) is not None:
            try:
                await self.context.tenant_manager.close_all()
            except Exception as e:
                self.logger.exception(f"Failed to close tenant_manager: {e}")
        for c in reversed(self._started_components):
            try:
                await c.close()
            except Exception as e:
                self.logger.exception(f"Failed to close {c.component_type.value}:{c.name}: {e}")
        self._started_components.clear()
        if self.context.thread_pool is not None:
            self.context.thread_pool.shutdown(wait=True)
            self.context.thread_pool = None

    async def update_component(self, component_enum: ComponentEnum | str, name: str, /, **kwargs) -> BaseComponent:
        """Update an existing component by type/name; never creates missing components."""
        component_enum = ComponentEnum(component_enum)
        group = self.context.components.get(component_enum)
        if not group or name not in group:
            raise KeyError(f"Component '{name}' not found in {component_enum.value}")

        component = group[name]
        for key, value in kwargs.items():
            if not hasattr(component, key):
                raise AttributeError(f"Component {component_enum.value}:{name} has no attribute '{key}'")
            setattr(component, key, value)
        return component

    # ----- Job execution -------------------------------------------------

    async def run_job(self, name: str, /, **kwargs) -> Response:
        """Execute a registered job by name and return its final Response."""
        if name not in self.context.jobs:
            raise KeyError(f"Job '{name}' not found")
        return await self.context.jobs[name](**kwargs)

    async def run_stream_job(self, name: str, /, **kwargs) -> AsyncGenerator[StreamChunk, None]:
        """Execute a streaming job, yielding chunks as they are produced."""
        if name not in self.context.jobs:
            raise KeyError(f"Job '{name}' not found")
        stream_queue: asyncio.Queue = asyncio.Queue()
        task = asyncio.create_task(self.context.jobs[name](stream_queue=stream_queue, **kwargs))
        async for chunk in execute_stream_task(
            stream_queue=stream_queue,
            task=task,
            task_name=name,
            output_format="chunk",
        ):
            assert isinstance(chunk, StreamChunk)
            yield chunk

    def run_app(self):
        """Serve the application through the configured service backend."""
        assert isinstance(self.context.service, BaseService)
        self.context.service.run_app(app=self)
