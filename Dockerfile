FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    HOST=0.0.0.0

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl util-linux \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 10001 mmagent \
    && useradd --system --uid 10001 --gid mmagent --home-dir /app --shell /usr/sbin/nologin mmagent

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY alembic.ini ./
COPY alembic ./alembic
COPY deploy/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

RUN pip install ".[postgres]" \
    && mkdir -p /app/data \
    && chown -R mmagent:mmagent /app \
    && chmod 0755 /usr/local/bin/docker-entrypoint.sh

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import os, urllib.request; port=os.environ.get('PORT','8080'); req=urllib.request.Request(f'http://127.0.0.1:{port}/health', method='HEAD'); urllib.request.urlopen(req, timeout=4).read()" || exit 1

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "-m", "mm_agent.main"]
