# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY pbi_viewer ./pbi_viewer

RUN uv sync --frozen --no-dev \
    && groupadd --gid 10001 pbirunner \
    && useradd --uid 10001 --gid pbirunner --no-create-home --shell /usr/sbin/nologin pbirunner \
    && mkdir -p /data \
    && chown pbirunner:pbirunner /data

USER pbirunner

VOLUME ["/data"]
EXPOSE 8765

CMD ["pbi-runner", "--host", "0.0.0.0", "--port", "8765", "--data-dir", "/data", "--no-browser"]
