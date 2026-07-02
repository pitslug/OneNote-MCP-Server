# OneNote MCP Sidekick — production image
# Multi-arch friendly (buildx sets TARGETPLATFORM); nothing personal baked in.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    ONENOTE_TRANSPORT=streamable-http \
    ONENOTE_HTTP_HOST=0.0.0.0 \
    ONENOTE_HTTP_PORT=8400 \
    ONENOTE_TOKEN_CACHE=/data/tokens/token_cache.json \
    ONENOTE_DATA_CACHE=/data/cache

WORKDIR /app

# uv for fast, reproducible installs. Deps layer first for build caching.
RUN pip install --no-cache-dir uv
RUN uv pip install --system --no-cache \
    "mcp>=1.2.0" \
    "fastmcp>=2.8,<3" \
    "msal>=1.25.0" \
    "httpx>=0.25.0" \
    "pillow>=10.0.0" \
    "uvicorn>=0.30"

# Application modules only (no tests, no local configs — see .dockerignore).
COPY onenote_mcp_server.py server_entry.py secrets_env.py inkml_raster.py notebook_cache.py ./
COPY pyproject.toml LICENSE ./

# Non-root, own the data volumes.
RUN useradd --system --uid 10001 --create-home app \
    && mkdir -p /data/tokens /data/cache \
    && chown -R app:app /app /data
USER app

EXPOSE 8400
VOLUME ["/data/tokens", "/data/cache"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os,sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.getenv('ONENOTE_HTTP_PORT','8400')+'/healthz', timeout=3).status==200 else 1)"

# Update this to your fork so the GHCR package links back to the repo.
LABEL org.opencontainers.image.source="https://github.com/pitslug/OneNote-MCP-Server" \
      org.opencontainers.image.description="OneNote MCP Sidekick — read handwritten ink, summarize, write back typed pages" \
      org.opencontainers.image.licenses="MIT"

CMD ["python", "server_entry.py"]
