# ReMe multi-tenant memory service.
#
# One container serves many tenants; each tenant is an ordinary workspace
# directory under /data/tenants (mount a volume there so memory persists).
# Tenant identity is taken from the X-Reme-Tenant header (trusted_header),
# so run this BEHIND your authenticated backend / gateway — do not expose the
# raw port to untrusted clients.
FROM python:3.11-slim

# PyPI index. Defaults to a fast China mirror for internal builds; override for a
# company mirror, e.g. --build-arg PIP_INDEX_URL=https://nexus.your-corp/repository/pypi/simple
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
ARG PIP_TRUSTED_HOST=mirrors.aliyun.com

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_TRUSTED_HOST=${PIP_TRUSTED_HOST} \
    PIP_DEFAULT_TIMEOUT=120 \
    PIP_RETRIES=8 \
    REME_WORKSPACES_ROOT=/data/tenants \
    REME_HOST=0.0.0.0 \
    REME_PORT=2333

WORKDIR /app

# Copy metadata first for better layer caching, then the source.
COPY pyproject.toml README.md ./
COPY reme ./reme

# Install base deps (from pyproject) + agentscope. That is ALL the multi-tenant
# default config needs: it uses the in-process agentscope agent backend, the
# `local` file_store / file_graph, and the `regex` tokenizer — none of which need
# faiss / jieba / rjieba / neo4j / networkx (those are lazy-imported, only when
# you switch to the faiss store / jieba tokenizer / neo4j graph backends).
# To enable those backends, add: pip install faiss-cpu jieba rjieba neo4j networkx
RUN pip install . && \
    pip install "agentscope==2.0.4.post1"

# Persistent per-tenant workspaces live here; mount a volume.
VOLUME ["/data"]
EXPOSE 2333

# Liveness: POST the auth-exempt /version endpoint.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request as u; u.urlopen(u.Request('http://127.0.0.1:2333/version', data=b'{}', headers={'Content-Type':'application/json'}), timeout=4)" || exit 1

# workspaces_root comes from REME_WORKSPACES_ROOT via the config template;
# host/port are set explicitly so the service binds all interfaces.
CMD ["sh", "-c", "reme start config=multi_tenant service.host=${REME_HOST} service.port=${REME_PORT}"]
