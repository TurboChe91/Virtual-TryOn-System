FROM python:3.12-slim

# curl is used by the container healthcheck only.
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY lunelle/ lunelle/
COPY pyproject.toml .

# The image carries lunelle/ but not .git, so `git rev-parse` cannot run inside the
# container. Bake the SHA at build time instead:
#   docker build --build-arg LUNELLE_BUILD_GIT_SHA="$(git rev-parse HEAD)" .
# Without it, executions record git_sha=NULL. That is honest but unhelpful --
# runtime_tree_sha still identifies the code, this only restores the readable label.
ARG LUNELLE_BUILD_GIT_SHA=""
ENV LUNELLE_BUILD_GIT_SHA=${LUNELLE_BUILD_GIT_SHA}

# Non-root runtime user; /data holds all persistent state (mount a volume).
RUN useradd --create-home --uid 10001 lunelle \
    && mkdir -p /data && chown -R lunelle:lunelle /data /app
USER lunelle

ENV LUNELLE_DATA_DIR=/data \
    LUNELLE_HOST=0.0.0.0 \
    LUNELLE_PORT=8300 \
    LUNELLE_ENV=production

EXPOSE 8300
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${LUNELLE_PORT:-8300}/health" || exit 1

# init applies migrations and verifies config before the server starts.
CMD ["sh", "-c", "python -m lunelle.cli init && exec python -m lunelle.cli serve"]
