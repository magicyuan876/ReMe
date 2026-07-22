# ReMe multi-tenant memory service.
#
# One container serves many tenants; each tenant is an ordinary workspace
# directory under /data/tenants (mount a volume there so memory persists).
# Tenant identity is taken from the X-Reme-Tenant header (trusted_header),
# so run this BEHIND your authenticated backend / gateway — do not expose the
# raw port to untrusted clients.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    REME_WORKSPACES_ROOT=/data/tenants \
    REME_HOST=0.0.0.0 \
    REME_PORT=2333

WORKDIR /app

# Copy metadata first for better layer caching, then the source.
COPY pyproject.toml README.md ./
COPY reme ./reme

# Install the package (base deps) plus the workspace-bound core deps the
# multi-tenant config actually uses. The codex / claude-code agent backends are
# disabled in the multi-tenant template, so their SDKs (openai-codex,
# claude-agent-sdk) are intentionally omitted to keep the image lean.
RUN pip install . && \
    pip install "agentscope==2.0.4.post1" faiss-cpu jieba rjieba neo4j networkx

# Persistent per-tenant workspaces live here; mount a volume.
VOLUME ["/data"]
EXPOSE 2333

# Liveness: POST the auth-exempt /version endpoint.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request as u; u.urlopen(u.Request('http://127.0.0.1:2333/version', data=b'{}', headers={'Content-Type':'application/json'}), timeout=4)" || exit 1

# workspaces_root comes from REME_WORKSPACES_ROOT via the config template;
# host/port are set explicitly so the service binds all interfaces.
CMD ["sh", "-c", "reme start config=multi_tenant service.host=${REME_HOST} service.port=${REME_PORT}"]
