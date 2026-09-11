# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Build stage. Compilers live here and nowhere else: psycopg, pillow and
# pdfplumber pull C extensions, and build-essential is ~200 MB of toolchain that
# has no business in a running container. Anything installed here reaches the
# runtime stage only by being copied deliberately.
# ---------------------------------------------------------------------------
FROM python:3.14-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Its own venv, so the runtime stage copies one self-contained directory rather
# than picking artefacts out of the system site-packages.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Dependencies before source: this layer is cached until the lock changes, so
# editing application code does not rebuild the whole dependency tree.
#
# requirements.lock is fully pinned, including transitives. pyproject.toml
# carries ranges, which is right for a library and wrong for an image -- a
# range means two builds of the same commit can install different code.
COPY requirements.lock ./
RUN pip install --upgrade pip && pip install -r requirements.lock

COPY pyproject.toml README.md alembic.ini ./
COPY src ./src
# --no-deps: every dependency is already installed from the lock. Without it pip
# re-resolves against the ranges in pyproject.toml and can silently upgrade past
# the pins that were just applied.
RUN pip install --no-deps .

# ---------------------------------------------------------------------------
# Runtime stage.
# ---------------------------------------------------------------------------
FROM python:3.14-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

# curl is kept for HEALTHCHECK; the compiler toolchain is not.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# A container that runs as root turns any code-execution bug into root inside
# the container, and shares a kernel with everything else on the host. There is
# nothing here that needs uid 0.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv

# No package installer in the runtime image. Nothing here runs pip after
# build -- uvicorn, alembic and the scripts are already installed -- and an
# attacker with a shell should not be able to fetch tooling. It also removes
# pip's vendored copies of msgpack and pkg_resources, which the image scanner
# reports against the versions in pip/_vendor/vendor.txt even though nothing
# outside pip can import them. Both the venv's pip and the base image's are
# removed; each carries its own vendor tree.
RUN /opt/venv/bin/python -m pip uninstall -y pip \
    && /usr/local/bin/python -m pip uninstall -y pip \
    && rm -rf /usr/local/lib/python3.12/ensurepip

COPY alembic.ini ./
COPY migrations ./migrations
# The evaluation harness runs as an ECS task: it builds the agent in-process
# and needs the database, which is private and unreachable from a runner.
COPY scripts ./scripts
COPY data ./data

# The local object store falls back to writing under data/uploads when S3 is
# unconfigured, so that path has to be writable by the unprivileged user. In
# deployment S3_BUCKET is set and nothing is written here.
RUN mkdir -p /app/data/uploads /app/data/processed \
    && chown -R appuser:appuser /app/data

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -f http://localhost:8000/health || exit 1

CMD ["uvicorn", "finance_rag.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
