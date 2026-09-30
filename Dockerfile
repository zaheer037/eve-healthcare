FROM python:3.12-slim-trixie AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv
WORKDIR /srv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY app ./app
COPY scripts ./scripts
COPY migrations ./migrations
COPY alembic.ini ./
RUN uv sync --frozen --no-dev

FROM builder AS test
ENV PATH="/srv/.venv/bin:$PATH"
RUN uv sync --frozen
COPY tests ./tests
CMD ["pytest", "-q"]

FROM python:3.12-slim-trixie
RUN useradd --uid 10001 --create-home app
WORKDIR /srv
# Root owns the code. The app user reads it and cannot rewrite it.
COPY --from=builder /srv /srv
ENV PATH="/srv/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER app
EXPOSE 8000
HEALTHCHECK --interval=5s --timeout=3s --start-period=10s --retries=10 CMD python -c "import urllib.request as u; u.urlopen('http://localhost:8000/health/live')"
CMD ["fastapi", "run", "--host", "0.0.0.0", "--port", "8000"]
