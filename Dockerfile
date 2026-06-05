# Evi server image — runs `evi web` on port 8000 with the scheduler.
#
# Two-stage: build wheels into a slim builder, copy into a minimal runtime.
# The image deliberately does NOT bundle an LLM backend (Ollama / LM
# Studio / llama.cpp). Point Evi at one with `EVI_LLM_BASE_URL` or by
# mounting a config file at /root/.evi/config.toml.
#
# Build:
#   docker build -t evi:latest .
# Run, talking to an Ollama on the host:
#   docker run --rm -p 8000:8000 \
#     -e LLM_BASE_URL=http://host.docker.internal:11434/v1 \
#     -e LLM_BACKEND=ollama \
#     -v evi-state:/root/.evi \
#     evi:latest

ARG PYTHON_VERSION=3.12-slim
FROM python:${PYTHON_VERSION} AS builder

WORKDIR /build
# Build dependencies for any wheels that don't ship a manylinux build.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential git \
    && rm -rf /var/lib/apt/lists/*

# Copy minimum needed for an editable install — we install proper at the
# end so changes to README/CHANGELOG don't bust the dep cache.
COPY pyproject.toml README.md LICENSE ./
COPY evi/ ./evi/

# Install with the extras that make the server useful out of the box.
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir ".[web,mcp,scheduler,downloads,web-tools]"


FROM python:${PYTHON_VERSION} AS runtime
LABEL org.opencontainers.image.title="Evi"
LABEL org.opencontainers.image.description="Local-first personal AI assistant — server image"
LABEL org.opencontainers.image.source="https://github.com/your-user/evi"

# Runtime-only deps: git for `evi worktree`, ca-certificates for HTTPS calls.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Bring in the site-packages + entry-point scripts from the builder.
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin/evi /usr/local/bin/evi

# Default HOME for Evi state. Mount a volume here in production.
ENV EVI_HOME=/root/.evi
VOLUME ["/root/.evi"]

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request, sys; \
sys.exit(0 if urllib.request.urlopen('http://localhost:8000/api/health', timeout=3).status == 200 else 1)" \
    || exit 1

CMD ["evi", "web", "--host", "0.0.0.0", "--port", "8000"]
