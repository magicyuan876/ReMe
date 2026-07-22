"""MCP service: expose jobs as MCP tools."""

from typing import TYPE_CHECKING, Any

from .base_service import BaseService
from .tenant_resolver import build_tenant_resolver
from ..component_registry import R
from ..job import BaseJob, StreamJob
from ...constants import REME_DEFAULT_HOST, REME_DEFAULT_PORT

if TYPE_CHECKING:
    from fastmcp.server.server import Transport
    from ...application import Application

# Tool-arg keys that would let a caller assert/spoof tenant identity.
_RESERVED_TENANT_KEYS = ("__tenant__", "tenant", "tenant_id")


@R.register("mcp")
class MCPService(BaseService):
    """Expose non-stream jobs as MCP tools over stdio, SSE, or streamable-http."""

    def __init__(
        self,
        transport: "Transport" = "sse",
        host: str = REME_DEFAULT_HOST,
        port: int = REME_DEFAULT_PORT,
        injected_job_kwargs: dict[str, Any] | None = None,
        tool_error_on_failure: bool = False,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.transport: Transport = transport
        self.host: str = host
        self.port: int = port
        self.injected_job_kwargs = dict(injected_job_kwargs or {})
        self.tool_error_on_failure = tool_error_on_failure
        # Multi-tenant boundary state, populated in build_service().
        self._mt_enabled: bool = False
        self._resolver = None
        self._exempt_jobs: set[str] = set()

    # ----- BaseService contract ------------------------------------------

    def build_service(self, app: "Application") -> None:
        """Construct the FastMCP server."""
        from fastmcp import FastMCP

        mt = getattr(app.config, "multi_tenant", None)
        self._mt_enabled = bool(mt and mt.enabled)
        if self._mt_enabled:
            self._resolver = build_tenant_resolver(mt.auth)
            self._exempt_jobs = set(mt.auth.exempt_jobs)
        self.service = FastMCP(
            name=app.config.app_name,
            lifespan=self._lifespan(app, self.host, self.port),
        )

    def _resolve_tenant(self) -> "str | None":
        """Resolve the tenant id from the current MCP HTTP request headers."""
        if self._resolver is None:
            return None
        try:
            from fastmcp.server.dependencies import get_http_headers

            headers = get_http_headers() or {}
        except Exception:  # pragma: no cover - transport without HTTP headers
            headers = {}
        return self._resolver.resolve(headers)

    def add_job(self, job: BaseJob) -> bool:
        """Register a non-stream job as an MCP tool; StreamJobs are unsupported."""
        from fastmcp.exceptions import ToolError
        from fastmcp.tools import FunctionTool

        if isinstance(job, StreamJob):
            return False

        async def execute_tool(**kwargs):
            conflicts = sorted(self.injected_job_kwargs.keys() & kwargs.keys())
            if conflicts:
                names = ", ".join(conflicts)
                raise ToolError(f"{names} injected by the MCP server and cannot be provided by the caller")
            kwargs.update(self.injected_job_kwargs)
            if self._mt_enabled and job.name not in self._exempt_jobs:
                present = [k for k in _RESERVED_TENANT_KEYS if k in kwargs]
                if present:
                    raise ToolError(f"Tenant fields are not accepted as tool arguments: {present}")
                tenant_id = self._resolve_tenant()
                if not tenant_id:
                    raise ToolError("Missing or invalid tenant credentials")
                kwargs["__tenant__"] = tenant_id
            response = await job(**kwargs)
            if self.tool_error_on_failure and not response.success:
                raise ToolError(str(response.answer))
            return response.answer

        parameters = dict(job.parameters or {})
        injected_names = self.injected_job_kwargs.keys()
        if "properties" in parameters:
            parameters["properties"] = {
                name: schema for name, schema in parameters["properties"].items() if name not in injected_names
            }
        if "required" in parameters:
            parameters["required"] = [name for name in parameters["required"] if name not in injected_names]

        self.service.add_tool(
            FunctionTool(
                name=job.name,
                description=job.description,
                fn=execute_tool,
                parameters=parameters,
            ),
        )
        return True

    def start_service(self, app: "Application") -> None:
        """Run the MCP server; bind host/port only for network transports."""
        if self._mt_enabled and self.transport == "stdio":
            raise ValueError(
                "multi_tenant mode is incompatible with the MCP 'stdio' transport "
                "(single connection, no per-request boundary); use streamable-http or sse",
            )
        transport_kwargs: dict = {}
        if self.transport != "stdio":
            transport_kwargs["host"] = self.host
            transport_kwargs["port"] = self.port
        self.service.run(transport=self.transport, show_banner=False, **transport_kwargs)
