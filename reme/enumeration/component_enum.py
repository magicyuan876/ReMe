"""Component enumeration module."""

from enum import Enum


class ComponentEnum(str, Enum):
    """Enumeration of component types for dependency injection and registration."""

    BASE = "base"

    AS_LLM = "as_llm"

    AS_EMBEDDING = "as_embedding"

    EMBEDDING_STORE = "embedding_store"

    FILE_CHUNKER = "file_chunker"

    FILE_STORE = "file_store"

    FILE_GRAPH = "file_graph"

    FILE_CATALOG = "file_catalog"

    KEYWORD_INDEX = "keyword_index"

    SERVICE = "service"

    CLIENT = "client"

    STEP = "step"

    JOB = "job"

    TOKENIZER = "tokenizer"

    AGENT_WRAPPER = "agent_wrapper"

    TENANT_RESOLVER = "tenant_resolver"


# Component types whose instances hold workspace-bound state or derive their
# behavior from the workspace (paths, cwd, job-tools). In multi-tenant mode the
# TenantManager instantiates one of each per active tenant so that reads/writes
# land in the tenant's own workspace. Everything NOT listed here is workspace-
# agnostic (pure inference clients / protocol front ends) and shared process-wide.
#
# The split axis is "does the component reference workspace_path / app_context.jobs?"
# — not "does it hold mutable state?". FILE_CHUNKER (to_workspace_relative) and
# AGENT_WRAPPER (cwd = workspace_path, resolves job-tools from app_context.jobs)
# are stateless yet workspace-bound, so they must be tenant-scoped.
TENANT_SCOPED_TYPES: frozenset[ComponentEnum] = frozenset(
    {
        ComponentEnum.FILE_STORE,
        ComponentEnum.KEYWORD_INDEX,
        ComponentEnum.FILE_GRAPH,
        ComponentEnum.FILE_CATALOG,
        ComponentEnum.EMBEDDING_STORE,
        ComponentEnum.FILE_CHUNKER,
        ComponentEnum.AGENT_WRAPPER,
    },
)
