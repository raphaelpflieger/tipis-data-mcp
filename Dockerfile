# tipis-data-mcp — the coworkers' read-only data connector. Runs as a non-root user on a read-only
# root filesystem (see docker-compose.yml); secrets are files mounted under /secrets.
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN groupadd --gid 10001 datamcp && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin datamcp
WORKDIR /app
COPY requirements.txt ./
# --require-hashes: the lockfile pins every wheel (regenerate with `make lock`, see README)
RUN pip install --require-hashes -r requirements.txt
COPY pyproject.toml README.md ./
COPY datamcp ./datamcp
# the package runs from /app (PYTHONPATH), no build step: python -m datamcp.server
ENV PYTHONPATH=/app
RUN mkdir -p /data /logs /secrets && chown datamcp:datamcp /data /logs
USER datamcp
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=8s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=6).status == 200 else 1)"
CMD ["python", "-m", "datamcp.server"]

# test image: the dev dependencies and the tests (docker compose -f docker-compose.test.yml run --rm tests)
FROM base AS test
USER root
COPY requirements-dev.txt ./
RUN pip install --require-hashes -r requirements-dev.txt
COPY tests ./tests
USER datamcp
CMD ["pytest", "-q", "-p", "no:cacheprovider"]
