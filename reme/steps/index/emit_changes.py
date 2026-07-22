"""Emit a ``changes`` batch from the paths a preceding file step touched.

Deployments that run without directory watchers (notably multi-tenant mode, where
per-tenant OS watchers are infeasible) index writes synchronously by chaining a
file-write step, this step, and ``update_index_step``. This step reads the paths a
write/edit/move/delete produced from the runtime context and appends them to
``changes`` as ``modified``; ``update_index_step`` reconciles each against the
filesystem, so a vanished path becomes a delete and a new one an add — one uniform
step covers create/update/rename/delete.
"""

from ..base_step import BaseStep
from ...components import R


@R.register("emit_change_step")
class EmitChangeStep(BaseStep):
    """Append context paths to the ``changes`` batch for downstream indexing."""

    _DEFAULT_KEYS = ("path", "src_path", "dst_path")

    async def execute(self):
        assert self.context is not None
        keys = self.kwargs.get("path_keys") or self._DEFAULT_KEYS
        paths: list[str] = []
        for key in keys:
            value = self.context.get(key)
            if isinstance(value, str) and value.strip():
                paths.append(value.strip())

        existing = self.context.get("changes") or []
        seen = {c.get("path") for c in existing if isinstance(c, dict)}
        merged = list(existing)
        for path in paths:
            if path not in seen:
                merged.append({"path": path, "change": "modified"})
                seen.add(path)
        self.context["changes"] = merged
        return self.context.response
