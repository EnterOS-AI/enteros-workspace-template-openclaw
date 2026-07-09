FROM python:3.11-slim

# openclaw CLI requires Node 22.12+ (per `npm install -g openclaw` engine check).
# Debian Bookworm/Trixie nodejs apt package ships v20.x — too old.
# Install Node 22 from NodeSource instead. Verify with `node --version` at build time.
# T4 escalation leg (RFC internal#456 §9 / PR#474):
#   sudo + util-linux(nsenter) + docker.io(CLI) are baked here so the
#   uid-1000 `agent` (see useradd below — UNCHANGED, agent stays
#   uid-1000) has a wired, audited path to host root inside the
#   provisioner's `--privileged --pid=host -v /:/host
#   -v /var/run/docker.sock:/var/run/docker.sock` container. Without
#   sudo, a uid-1000 process in --privileged CANNOT nsenter/chroot
#   /host (--privileged grants caps to root, not uid-1000) and cannot
#   use the root:docker 0660 docker.sock — T4 would be
#   provisioner-shape-only (the documented ABSENT-escalation-leg gap).
#   Mirrors template-claude-code (proven, live-verified — ECR
#   sha256:e1ae9795… / git bbc2dae). The sudoers drop-in + docker-group
#   add are below, after useradd. ADDITIVE: does NOT change the agent
#   uid and does NOT change /configs token ownership (still agent-owned,
#   enforced by entrypoint.sh + the t4-conformance gate).
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl gosu ca-certificates gnupg sudo util-linux docker.io \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && node --version | grep -E '^v22\.' || (echo "::error::expected Node 22, got $(node --version)" >&2; exit 1)

# Install OpenClaw CLI. Fail loud if install or version probe fails — previously
# this had `|| true` which silently shipped a broken image (see internal#418).
RUN npm install -g openclaw \
    && openclaw --version

# Create agent user — UNCHANGED. The agent runs as uid-1000; the T4
# escalation leg below is additive and does NOT promote the agent to
# root. /configs/.auth_token must stay agent-readable (Hermes
# list_peers 401 class — RFC internal#456 §10), enforced by
# entrypoint.sh chowning /configs before `gosu agent`.
RUN useradd -u 1000 -m -s /bin/bash agent

# --- T4 escalation leg (RFC internal#456 §9.3 / PR#474) ---
# Wired path: uid-1000 agent -> host root inside the provisioner's
# --privileged --pid=host -v /:/host -v docker.sock container.
#   1. NOPASSWD sudoers drop-in (mode 0440, visudo-validated at build
#      so a malformed sudoers can never ship a broken-sudo image).
#   2. agent in the `docker` group so the bind-mounted root:docker
#      0660 /var/run/docker.sock is usable without sudo.
# Atomic co-sequencing (RFC §10): this ships in the SAME image
# revision as the uid-1000 + agent-readable-token entrypoint contract;
# the t4-conformance gate asserts BOTH on the running container.
RUN set -eux; \
    printf 'agent ALL=(ALL) NOPASSWD:ALL\n' > /etc/sudoers.d/agent-t4; \
    chmod 0440 /etc/sudoers.d/agent-t4; \
    visudo -cf /etc/sudoers.d/agent-t4; \
    groupadd -f docker; \
    usermod -aG docker agent; \
    id agent

# --- Pre-bake the org-management MCP for DETERMINISTIC concierge warm-up (core#3082). ---
# The kind=platform concierge's management MCP is delivered as a plugin
# (molecule-ai-plugin-molecule-platform-mcp) whose settings-fragment launches
#   npx --prefer-offline @molecule-ai/mcp-server@<ver>
# On a FRESH concierge that npx would otherwise COLD-PULL the full ~100-dep tree
# from the Cloudflare-fronted Gitea npm registry — a network fetch that races the
# runtime readiness probe's per-server 20s handshake budget and, under CF-WAF
# throttling / concurrent-npx contention, can blow past the readiness window
# entirely (observed: a fresh concierge stuck 503 past 300s while a warm one
# reached its tools in ~48s — the whole "flaky warm-up" is that ONE network pull).
#
# Baking the exact version + its dep tree into the AGENT user's npm caches at
# BUILD time makes the runtime's `npx --prefer-offline` resolve ENTIRELY FROM
# CACHE — ZERO network pull — so warm-up is fast + deterministic every time.
# `--prefer-offline` (set in the plugin fragment) keeps the registry as a
# SELF-HEALING fallback if the cache ever misses (older image, cache evicted).
#
# ORDERING (fix vs the #210 claude-code block): the `npm install` into $warm only
# seeds the CONTENT cache (_cacache tarballs) — it does NOT create the npx run
# cache (_npx). npx --offline resolves a package via _npx + the cached PACKUMENT;
# with neither present it dies `ENOTCACHED: cache mode is only-if-cached but no
# cached response is available`. If the seeding `npx --prefer-offline` runs while
# cwd=$warm (node_modules still present) npx executes the LOCAL copy and never
# builds an _npx entry, so a later --offline resolve fails. We therefore DISCARD
# $warm FIRST, then run the seeding `npx --prefer-offline` from a clean cwd so it
# actually creates the _npx entry + caches the packument — making the strict
# --offline self-check (and the runtime --prefer-offline) resolve with zero
# network. Verified deterministic (3/3 fresh HOMEs) in node:22.
#
# HYGIENE: we warm only the caches (throwaway install, then discard node_modules)
# — the admin MCP is NOT globally installed and NOT on PATH here, so an ordinary
# (non-concierge) workspace on this shared image gains only inert cached tarballs,
# never an active admin tool surface (the tools require MOLECULE_MCP_MODE=management
# + a CP-authenticated bearer, injected only into the concierge). Run as `agent` so
# the cache lands in the SAME /home/agent/.npm the gosu-dropped runtime reads at boot.
#
# MCP_SERVER_VERSION MUST match the plugin fragment's pinned version
# (molecule-ai-plugin-molecule-platform-mcp settings-fragment.json). A stale bake
# does NOT merely forfeit determinism: when the plugin pins AHEAD of this baked
# version the runtime's `npx --prefer-offline @molecule-ai/mcp-server@<PIN>`
# MISSES the <BAKED>-only cache and COLD-PULLS <PIN>, which under CF-WAF
# throttling hard-fails ETARGET -> the concierge FALSE-READYs on every bump
# (#228). GUARD D (task #229) enforces this lockstep as a HARD CI gate
# (scripts/lint-mcp-pin-lockstep.sh, wired into ci.yml) and the publish workflow
# OVERRIDES this default with the SSOT-derived --build-arg MCP_SERVER_VERSION so
# the pushed image is ALWAYS built with the plugin-pinned version. This default
# is kept in lockstep for plain/local `docker build` (no --build-arg) and is
# machine-checked against the plugin SSOT on every PR.
ARG MCP_SERVER_VERSION=1.8.2
USER agent
RUN set -eux; \
    mkdir -p /home/agent/.npm; \
    printf '@molecule-ai:registry=https://git.moleculesai.app/api/packages/molecule-ai/npm/\n' > /home/agent/.npmrc; \
    warm="$(mktemp -d)"; cd "$warm"; npm init -y >/dev/null 2>&1; \
    npm install --no-audit --no-fund --loglevel=error "@molecule-ai/mcp-server@${MCP_SERVER_VERSION}"; \
    cd /; rm -rf "$warm"; \
    printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"prebake","version":"1"}}}' \
      | MOLECULE_MCP_MODE=management timeout 60 npx -y --prefer-offline "@molecule-ai/mcp-server@${MCP_SERVER_VERSION}" >/dev/null 2>&1 || true; \
    printf '%s\n%s\n' \
      '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"verify","version":"1"}}}' \
      '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
      | MOLECULE_MCP_MODE=management timeout 60 npx -y --offline "@molecule-ai/mcp-server@${MCP_SERVER_VERSION}" 2>/dev/null | grep -q provision_workspace \
      || (echo "ERROR: pre-baked @molecule-ai/mcp-server@${MCP_SERVER_VERSION} did not resolve OFFLINE or provision_workspace missing — the concierge warm-up bake is broken" >&2 && exit 1)
USER root

WORKDIR /app

# RUNTIME_VERSION is forwarded from molecule-ci's reusable publish
# workflow as a docker build-arg. Cascade-triggered builds set it to
# the exact runtime version PyPI just published. Including it as an
# ARG changes the cache key for the pip install layer below — the
# fix for the cascade cache trap that bit us 5x on 2026-04-27.
ARG RUNTIME_VERSION=

# Gitea PyPI registry is the PRIMARY internal index per RFC internal#596
# (Gitea PyPI middleman; CTO GO'd 2026-05-19). Anonymous reads work because
# `molecule-ai` is a public org — no auth needs to be wired into the build.
# pypi.org is kept as best-effort fallback for transitive deps that are
# only on PyPI (everything-except-our-runtime). This removes the vendor
# SPOF that bit us 2026-05-19 (compounded PyPI abuse-block + Railway
# outage; internal#593 + #595) and unblocks publishes of versions that
# Gitea-only has (e.g. workspace-runtime 0.1.1013+ / 0.2.0+).
ARG PIP_INDEX_URL=https://git.moleculesai.app/api/packages/molecule-ai/pypi/simple/
ARG PIP_EXTRA_INDEX_URL=https://pypi.org/simple/

COPY requirements.txt .
RUN pip install --no-cache-dir \
      --index-url "${PIP_INDEX_URL}" \
      --extra-index-url "${PIP_EXTRA_INDEX_URL}" \
      -r requirements.txt && \
    if [ -n "${RUNTIME_VERSION}" ]; then \
      pip install --no-cache-dir --upgrade \
        --index-url "${PIP_INDEX_URL}" \
        --extra-index-url "${PIP_EXTRA_INDEX_URL}" \
        "molecules-workspace-runtime==${RUNTIME_VERSION}"; \
    fi

COPY adapter.py .
COPY __init__.py .

# Generic GIT_ASKPASS helper. Reads HTTPS Basic-Auth credentials from
# env vars (GIT_HTTP_USERNAME / GIT_HTTP_PASSWORD, with GITEA_USER /
# GITEA_TOKEN as fallback) and emits them on the git credential-prompt
# protocol, so container-side `git` can authenticate to any private
# HTTPS remote without on-disk .gitconfig / .git-credentials mutation.
# Installed as /usr/local/bin/molecule-askpass — the platform-side
# provisioner sets GIT_ASKPASS to that path. Script body contains no
# hostnames or vendor literals; the deployer decides which remote the
# credentials apply to by virtue of populating those env vars.
COPY scripts/molecule-askpass /usr/local/bin/molecule-askpass
RUN chmod +x /usr/local/bin/molecule-askpass

ENV ADAPTER_MODULE=adapter

# molecule-runtime previously ran as root (no entrypoint wrapper), which
# made list_peers work only "by accident (root==root)". The T4
# escalation leg requires uid-1000; entrypoint.sh atomically drops to
# uid-1000 `agent` AND chowns /configs so /configs/.auth_token stays
# agent-readable (RFC internal#456 §9-11). See entrypoint.sh header.
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
