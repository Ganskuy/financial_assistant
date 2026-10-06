FROM python:3.11-slim-bookworm AS builder
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
WORKDIR /build
COPY requirements.txt ./
RUN python -m venv /opt/venv && /opt/venv/bin/pip install --no-compile -r requirements.txt

FROM python:3.11-slim-bookworm AS runtime
ENV PATH="/opt/venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN groupadd --gid 10001 finance && useradd --uid 10001 --gid finance --no-create-home finance
WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY --chown=finance:finance app ./app
COPY --chown=finance:finance alembic ./alembic
COPY --chown=finance:finance alembic.ini ./
COPY --chown=finance:finance scripts ./scripts
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--no-proxy-headers"]
