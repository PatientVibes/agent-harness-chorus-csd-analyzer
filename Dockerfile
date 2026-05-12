# ── Stage 1: dependency builder ───────────────────────────────────────────────
FROM python:3.12-slim AS builder

# Install uv
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /uvx /usr/local/bin/

WORKDIR /build

# Copy workspace manifests and lockfile first (maximizes layer caching)
COPY pyproject.toml uv.lock ./
COPY web/pyproject.toml ./web/

# Copy source for the packages
COPY chorus_forms/ ./chorus_forms/
COPY web/web/ ./web/web/

# Install all workspace dependencies (no dev extras)
RUN uv sync --frozen --no-dev --package chorus-forms-web

# ── Stage 2: runtime image ────────────────────────────────────────────────────
FROM python:3.12-slim

# Build metadata
ARG VERSION=0.2.0
ARG GIT_SHA=unknown
LABEL org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.title="chorus-forms-web" \
      org.opencontainers.image.description="CSD Converter Web UI with AI-assisted analysis"

# Create non-root user
RUN useradd --create-home --shell /bin/bash appuser

WORKDIR /app

# Copy virtualenv from builder
COPY --from=builder /build/.venv /app/.venv

# Copy application source
COPY chorus_forms/ /app/chorus_forms/
COPY web/web/ /app/web/

# Temp directory for conversion sessions
RUN mkdir -p /tmp/conversions && chown appuser:appuser /tmp/conversions

USER appuser

ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH="/app"
ENV APP_VERSION="${VERSION}"
ENV APP_GIT_SHA="${GIT_SHA}"

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8000/health').raise_for_status()"

CMD ["python", "-m", "uvicorn", "web.app:app", "--host", "0.0.0.0", "--port", "8000", "--graceful-timeout", "60"]
