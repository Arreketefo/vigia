FROM python:3.12-slim

# uv for fast, reproducible installs
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Dependency layer (cached unless lockfile or vendored core changes).
# vendor/ holds the private radar-core lib (path dependency): it must exist
# BEFORE the first sync.
COPY pyproject.toml uv.lock ./
COPY vendor ./vendor
RUN uv sync --frozen --no-dev --no-install-project

# App layer
COPY . .
RUN uv sync --frozen --no-dev

# From here on, uv run must NOT re-sync: it would pull dev deps from PyPI
# at container runtime (crash-loop without network, slow healthchecks).
ENV UV_NO_SYNC=1

# Long-running scheduler daemon
CMD ["uv", "run", "python", "-m", "vigia"]
