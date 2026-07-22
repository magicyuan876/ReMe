"""Shared component wiring: workspace setup, registry instantiation, topo ordering.

Extracted from ``Application`` so that both the single-workspace ``Application`` and
the per-tenant ``TenantManager`` construct component sets through one code path.
"""

import heapq
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar

from ..enumeration import ComponentEnum
from ..schema import ApplicationConfig, ComponentConfig

if TYPE_CHECKING:
    from .application_context import ApplicationContext
    from .base_component import BaseComponent
    from .tenant_context import TenantContext

T = TypeVar("T", bound="BaseComponent")
_NodeKey = tuple[ComponentEnum, str]


def workspace_subdirs(cfg: ApplicationConfig) -> list[str]:
    """Configured workspace subdirectories that must exist under the workspace root."""
    return [
        d
        for d in (
            cfg.metadata_dir,
            cfg.session_dir,
            cfg.mem_session_dir,
            cfg.resource_dir,
            cfg.daily_dir,
            cfg.digest_dir,
        )
        if d
    ]


def ensure_workspace(workspace_dir: str | Path, cfg: ApplicationConfig) -> Path:
    """Create the workspace root and its configured subdirectories; return the abs root."""
    root = Path(workspace_dir).absolute()
    root.mkdir(parents=True, exist_ok=True)
    for subdir in workspace_subdirs(cfg):
        (root / subdir).mkdir(parents=True, exist_ok=True)
    return root


def instantiate(
    ctype: ComponentEnum,
    cfg: ComponentConfig,
    app_context: "ApplicationContext | TenantContext",
    *,
    label: str,
    expected_type: type[T],
    name: str | None = None,
) -> T:
    """Resolve ``cfg.backend`` through the registry and construct the instance.

    ``app_context`` is injected into the constructor so the instance derives its
    workspace/paths/dependencies from that context — an ``ApplicationContext`` for
    the shared process, or a ``TenantContext`` for a per-tenant component.
    """
    from .component_registry import R  # lazy: registry self-populates on module import

    if not cfg.backend:
        raise ValueError(f"{label} is missing the required 'backend' field")
    backend_cls = R.get(ctype, cfg.backend)
    if backend_cls is None:
        raise ValueError(f"Unregistered backend '{cfg.backend}' for {label}")

    params = cfg.model_dump()
    params["app_context"] = app_context
    if name is not None:
        params.setdefault("name", name)
    instance = backend_cls(**params)
    if not isinstance(instance, expected_type):
        got, want = type(instance).__name__, expected_type.__name__
        raise TypeError(f"{label} backend '{cfg.backend}' produced {got}, expected {want} subclass")
    return instance


def _build_dependency_graph(
    nodes: dict[_NodeKey, "BaseComponent"],
    external: set[_NodeKey] | None = None,
) -> tuple[dict[_NodeKey, int], dict[_NodeKey, list[_NodeKey]]]:
    """Compute in-degree and adjacency lists; raise if a required dep is missing.

    ``external`` names dependency keys that are satisfied outside this graph (e.g.
    globally shared components a tenant subgraph binds). A required dep resolving
    there adds no in-edge and is not an error.
    """
    external = external or set()
    in_degree: dict[_NodeKey, int] = dict.fromkeys(nodes, 0)
    dependents: dict[_NodeKey, list[_NodeKey]] = {k: [] for k in nodes}
    for key, comp in nodes.items():
        for dep in comp.dependencies:
            dep_key = (dep.ctype, dep.name)
            if dep_key in nodes:
                dependents[dep_key].append(key)
                in_degree[key] += 1
            elif dep_key in external or dep.optional:
                continue
            else:
                raise ValueError(
                    f"Component {key[0].value}:{key[1]} depends on unregistered {dep.ctype.value}:{dep.name}",
                )
    return in_degree, dependents


def topological_order(
    components: dict[ComponentEnum, dict[str, "BaseComponent"]],
    external: dict[ComponentEnum, dict[str, "BaseComponent"]] | None = None,
) -> list["BaseComponent"]:
    """Return the given components in dependency order via Kahn's algorithm.

    ``external`` are components satisfied outside this graph (globally shared ones a
    tenant subgraph binds): a required dep resolving there is accepted and adds no
    in-edge. Required deps that resolve nowhere are raised here.
    """
    nodes: dict[_NodeKey, "BaseComponent"] = {
        (ctype, name): comp for ctype, group in components.items() for name, comp in group.items()
    }
    external_keys: set[_NodeKey] = (
        {(ctype, name) for ctype, group in external.items() for name in group} if external else set()
    )
    in_degree, dependents = _build_dependency_graph(nodes, external_keys)

    ready = [k for k, d in in_degree.items() if d == 0]
    heapq.heapify(ready)
    ordered: list["BaseComponent"] = []
    while ready:
        key = heapq.heappop(ready)
        ordered.append(nodes[key])
        for downstream in dependents[key]:
            in_degree[downstream] -= 1
            if in_degree[downstream] == 0:
                heapq.heappush(ready, downstream)

    if len(ordered) != len(nodes):
        unresolved = [f"{k[0].value}:{k[1]}" for k, d in in_degree.items() if d > 0]
        raise ValueError(f"Circular dependency detected among: {unresolved}")
    return ordered
