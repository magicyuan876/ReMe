"""Streaming job for real-time output delivery."""

from .base_job import BaseJob
from ..component_registry import R
from ..runtime_context import RuntimeContext
from ...enumeration import ChunkEnum


@R.register("stream")
class StreamJob(BaseJob):
    """Job that streams chunks to a queue instead of returning a Response."""

    async def __call__(self, **kwargs) -> None:
        """Run steps; emit failures as ERROR chunks, then a terminal DONE marker."""
        tenant_id = kwargs.pop("__tenant__", None)
        merged = {**self.kwargs, **kwargs}
        tenant_context = None
        if tenant_id is not None:
            tenant_context = await self._acquire_tenant_context(tenant_id)
            tenant_context.in_flight += 1
        context = RuntimeContext(**merged)
        if tenant_id is not None:
            context["tenant_id"] = tenant_id
        try:
            steps = self._build_steps(app_context=tenant_context) if tenant_context is not None else self._build_steps()
            for step in steps:
                await step(context)
            if tenant_context is not None:
                tenant_context.dirty = True
        except Exception as e:
            await context.add_stream_string(str(e), ChunkEnum.ERROR)
        finally:
            if tenant_context is not None:
                tenant_context.in_flight -= 1
            # Always emit DONE so consumers can detach even after an error.
            await context.add_stream_done()
