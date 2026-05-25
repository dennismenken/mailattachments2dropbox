# syntax=docker/dockerfile:1.7
FROM python:3.13-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_LINK_MODE=copy

# Bring in uv from the official distroless image.
COPY --from=ghcr.io/astral-sh/uv:0.7.20 /uv /usr/local/bin/uv

# Non-root user for the runtime.
RUN groupadd --system app && useradd --system --gid app --create-home --uid 1000 app

WORKDIR /app

# Install runtime dependencies first so layer caching kicks in.
COPY pyproject.toml uv.lock README.md /app/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# Copy source code and install the project itself.
COPY src /app/src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# Logs are written to a bind-mounted volume so they survive container restarts.
RUN mkdir -p /app/logs && chown -R app:app /app/logs

USER app

ENTRYPOINT ["uv", "run", "--no-sync", "python", "-m", "mailattachments2dropbox"]
