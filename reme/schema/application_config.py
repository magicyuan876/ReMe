"""Application configuration models."""

import os

from pydantic import BaseModel, ConfigDict, Field

from ..enumeration import ComponentEnum


class ComponentConfig(BaseModel):
    """Base config for a component; extra fields allowed for backend-specific options."""

    model_config = ConfigDict(extra="allow")

    backend: str = Field(default="", description="Backend implementation class name")


class JobConfig(ComponentConfig):
    """Config for a job — an ordered sequence of step components. Keyed by name in ApplicationConfig.jobs."""

    description: str = Field(default="", description="Human-readable description")
    parameters: dict = Field(default_factory=dict, description="Job-level parameters")
    steps: list[ComponentConfig] = Field(default_factory=list, description="Ordered step configs")
    enable_serve: bool = Field(default=True, description="Whether to expose this job through the service layer")


class MultiTenantAuthConfig(BaseModel):
    """Service-boundary tenant identity resolution config."""

    model_config = ConfigDict(extra="allow")

    backend: str = Field(
        default="trusted_header",
        description="TenantResolver backend: 'trusted_header' (behind a trusted gateway) or 'static_token_map'",
    )
    header: str = Field(default="X-Reme-Tenant", description="Header carrying the tenant id (trusted_header backend)")
    token_header: str = Field(
        default="Authorization",
        description="Header carrying the bearer credential (static_token_map backend)",
    )
    tokens: dict[str, str] = Field(
        default_factory=dict,
        description="credential -> tenant_id map (static_token_map backend); values support ${ENV} expansion",
    )
    exempt_jobs: list[str] = Field(
        default_factory=lambda: ["version", "health_check", "help"],
        description="Tenant-agnostic ops jobs served without authentication",
    )


class MultiTenantConfig(BaseModel):
    """Opt-in multi-tenant mode: one process serves many tenants, each a workspace under workspaces_root."""

    model_config = ConfigDict(extra="allow")

    enabled: bool = Field(default=False, description="Enable multi-tenant mode; default off preserves single-workspace")
    workspaces_root: str = Field(default="", description="Root dir holding one workspace subdir per tenant; required")
    max_active_tenants: int = Field(default=300, description="LRU bound on resident (started) tenant component sets")
    tenant_idle_close_seconds: float = Field(default=1800.0, description="Idle seconds before eviction; 0 disables")
    idle_sweep_seconds: float = Field(default=60.0, description="Interval of the idle-eviction maintenance sweep")
    tenant_id_pattern: str = Field(
        default=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$",
        description="Regex a tenant id must match; forbids path separators and a leading dot",
    )
    consolidation_enabled: bool = Field(
        default=True,
        description="Run the ReMe-side per-tenant auto_dream scheduler (replaces the disabled dream_cron)",
    )
    consolidation_job: str = Field(default="auto_dream", description="Job name the consolidation scheduler invokes")
    consolidation_interval_seconds: float = Field(
        default=3600.0,
        description="How often the consolidation scheduler scans tenants on disk for new daily notes",
    )
    consolidation_concurrency: int = Field(
        default=2,
        description="Max tenants consolidated (auto_dream) concurrently per scan",
    )
    auth: MultiTenantAuthConfig = Field(default_factory=MultiTenantAuthConfig, description="Boundary identity config")


class ApplicationConfig(BaseModel):
    """Root config for the ReMe application."""

    app_name: str = Field(default=os.getenv("APP_NAME", "ReMe"), description="Application display name")
    environment: dict[str, str] = Field(
        default_factory=dict,
        description="Environment variables loaded once at startup and passed to agent subprocesses",
    )
    workspace_dir: str = Field(default=".reme", description="Workspace root directory for runtime files")
    metadata_dir: str = Field(default="metadata", description="Subdirectory for ReMe persistent state")
    session_dir: str = Field(default="session", description="Subdirectory for persisted agent sessions")
    mem_session_dir: str = Field(default="mem_session", description="Subdirectory for persisted agent sessions")
    resource_dir: str = Field(default="resource", description="Subdirectory for external assets")
    daily_dir: str = Field(default="daily", description="Subdirectory for daily memory")
    digest_dir: str = Field(default="digest", description="Subdirectory for digest memory")
    enable_logo: bool = Field(default=True, description="Show ASCII logo on startup")
    timezone: str | None = Field(default="Asia/Shanghai", description="IANA timezone; None uses local time")
    language: str = Field(default="", description="Default language for LLM interactions")
    log_to_console: bool = Field(default=True, description="Log to console")
    log_to_file: bool = Field(default=True, description="Log to file")
    mcp_servers: dict[str, dict] = Field(default_factory=dict, description="MCP server configs by name")
    service: ComponentConfig = Field(default_factory=ComponentConfig, description="Service endpoint config")
    jobs: dict[str, JobConfig] = Field(default_factory=dict, description="Job definitions keyed by job name")
    thread_pool_max_workers: int = Field(default=0, description="Max worker threads; 0 to disable")
    components: dict[ComponentEnum, dict[str, ComponentConfig]] = Field(
        default_factory=dict,
        description="Component registry keyed by type then name",
    )
    multi_tenant: MultiTenantConfig = Field(
        default_factory=MultiTenantConfig,
        description="Multi-tenant mode config; disabled by default",
    )
