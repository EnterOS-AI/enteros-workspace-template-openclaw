FROM python:3.11-slim

# openclaw CLI requires Node 22.12+ (per `npm install -g openclaw` engine check).
# Debian Bookworm/Trixie nodejs apt package ships v20.x — too old.
# Install Node 22 from NodeSource instead. Verify with `node --version` at build time.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl gosu ca-certificates gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && node --version | grep -E '^v22\.' || (echo "::error::expected Node 22, got $(node --version)" >&2; exit 1)

# Install OpenClaw CLI. Fail loud if install or version probe fails — previously
# this had `|| true` which silently shipped a broken image (see internal#418).
RUN npm install -g openclaw \
    && openclaw --version

RUN useradd -u 1000 -m -s /bin/bash agent
WORKDIR /app

# RUNTIME_VERSION is forwarded from molecule-ci's reusable publish
# workflow as a docker build-arg. Cascade-triggered builds set it to
# the exact runtime version PyPI just published. Including it as an
# ARG changes the cache key for the pip install layer below — the
# fix for the cascade cache trap that bit us 5x on 2026-04-27.
ARG RUNTIME_VERSION=

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    if [ -n "${RUNTIME_VERSION}" ]; then \
      pip install --no-cache-dir --upgrade "molecule-ai-workspace-runtime==${RUNTIME_VERSION}"; \
    fi

COPY adapter.py .
COPY __init__.py .

ENV ADAPTER_MODULE=adapter

ENTRYPOINT ["molecule-runtime"]
