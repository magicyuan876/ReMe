"""HTTP service: exposes jobs as FastAPI endpoints (JSON, or SSE for stream jobs)."""

import asyncio
import warnings
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi import Request as HTTPRequest
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from .base_service import BaseService
from .tenant_resolver import build_tenant_resolver
from ..component_registry import R
from ..job import BaseJob, StreamJob
from ...constants import REME_DEFAULT_HOST, REME_DEFAULT_PORT
from ...schema import Request, Response
from ...utils import execute_stream_task

# Body keys that would let a caller assert or spoof tenant identity; identity must
# come from the authenticated boundary (headers), never the payload.
_RESERVED_TENANT_KEYS = ("__tenant__", "tenant", "tenant_id")

if TYPE_CHECKING:
    from ...application import Application


# uvicorn 0.41 still imports these deprecated websockets symbols on startup,
# even though we don't use WebSocket. Silence just those specific warnings.
_WEBSOCKET_DEPRECATION_PATTERNS = (
    r".*websockets\.legacy is deprecated.*",
    r".*WebSocketServerProtocol is deprecated.*",
)


@R.register("http")
class HttpService(BaseService):
    """Map non-stream jobs to JSON POST endpoints and StreamJobs to SSE endpoints."""

    def __init__(self, host: str = REME_DEFAULT_HOST, port: int = REME_DEFAULT_PORT, **kwargs):
        super().__init__(**kwargs)
        self.host: str = host
        self.port: int = port
        # Multi-tenant boundary state, populated in build_service().
        self._mt_enabled: bool = False
        self._resolver = None
        self._exempt_jobs: set[str] = set()

    # ----- BaseService contract ------------------------------------------

    def build_service(self, app: "Application") -> None:
        """Create the FastAPI app with permissive CORS and an app-managed lifespan."""
        mt = getattr(app.config, "multi_tenant", None)
        self._mt_enabled = bool(mt and mt.enabled)
        if self._mt_enabled:
            self._resolver = build_tenant_resolver(mt.auth)
            self._exempt_jobs = set(mt.auth.exempt_jobs)
        self.service = FastAPI(
            title=app.config.app_name,
            lifespan=self._lifespan(app, self.host, self.port),
        )
        cors_origins = ["*"]
        self.service.add_middleware(
            CORSMiddleware,  # type: ignore[arg-type]
            allow_origins=cors_origins,
            allow_credentials="*" not in cors_origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    def add_job(self, job: BaseJob) -> bool:
        """Dispatch to streaming or non-streaming registration based on job type."""
        if isinstance(job, StreamJob):
            self._add_stream_job(job)
        else:
            self._add_json_job(job)
        return True

    def start_service(self, app: "Application") -> None:
        """Run uvicorn, suppressing unrelated websocket deprecation noise."""
        for pattern in _WEBSOCKET_DEPRECATION_PATTERNS:
            warnings.filterwarnings("ignore", category=DeprecationWarning, message=pattern)
        uvicorn.run(self.service, host=self.host, port=self.port, **self.kwargs)

    # ----- Endpoint factories --------------------------------------------

    def _boundary_kwargs(self, job: BaseJob, request: Request, http_request: HTTPRequest) -> dict:
        """Build job kwargs, enforcing the tenant boundary in multi-tenant mode.

        Rejects tenant fields in the body (400), resolves the tenant from headers
        (401 on failure), and injects the reserved ``__tenant__`` kwarg. Tenant-agnostic
        ops jobs (configured exempt list) are served without authentication.
        """
        payload = request.model_dump(exclude_none=True)
        if not self._mt_enabled:
            return payload
        present = [k for k in _RESERVED_TENANT_KEYS if k in payload]
        if present:
            raise HTTPException(
                status_code=400,
                detail=f"Tenant fields are not accepted in the request body: {present}",
            )
        if job.name in self._exempt_jobs:
            return payload
        tenant_id = self._resolver.resolve(http_request.headers) if self._resolver else None
        if not tenant_id:
            raise HTTPException(status_code=401, detail="Missing or invalid tenant credentials")
        payload["__tenant__"] = tenant_id
        return payload

    def _add_json_job(self, job: BaseJob) -> None:
        """Register a job as POST /{job.name} returning a JSON Response."""

        async def endpoint(request: Request, http_request: HTTPRequest) -> Response:
            return await job(**self._boundary_kwargs(job, request, http_request))

        self.service.post(
            f"/{job.name}",
            response_model=Response,
            description=job.description,
        )(endpoint)

    def _add_stream_job(self, job: StreamJob) -> None:
        """Register a StreamJob as POST /{job.name} streaming chunks as text/event-stream."""

        async def endpoint(request: Request, http_request: HTTPRequest) -> StreamingResponse:
            job_kwargs = self._boundary_kwargs(job, request, http_request)
            stream_queue: asyncio.Queue = asyncio.Queue()
            task = asyncio.create_task(
                job(stream_queue=stream_queue, **job_kwargs),
            )

            async def body() -> AsyncGenerator[bytes, None]:
                async for chunk in execute_stream_task(
                    stream_queue=stream_queue,
                    task=task,
                    task_name=job.name,
                    output_format="bytes",
                ):
                    assert isinstance(chunk, bytes)
                    yield chunk

            return StreamingResponse(body(), media_type="text/event-stream")

        self.service.post(f"/{job.name}")(endpoint)
