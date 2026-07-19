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
#   Mirrors the live-verified template-claude-code T4 contract. The
#   sudoers drop-in + docker-group
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
    groupadd -g 988 -f docker-host || true; \
    usermod -aG docker agent; \
    usermod -aG docker-host agent || true; \
    id agent

WORKDIR /app

# RUNTIME_VERSION is forwarded from this repository's publish-image
# workflow as a docker build-arg. Cascade-triggered builds set it to
# the exact runtime version the private registry just published. Including it as an
# ARG changes the cache key for the pip install layer below — the
# fix for the cascade cache trap that bit us 5x on 2026-04-27.
ARG RUNTIME_VERSION=

# Acquire the private runtime from the Gitea package registry before resolving
# its public dependencies. Keeping the private and public indexes in separate
# pip commands prevents dependency-confusion candidates from entering the
# runtime wheel lookup. Anonymous reads are supported for this public org.
ARG MOLECULE_RUNTIME_INDEX=https://git.moleculesai.app/api/packages/molecule-ai/pypi/simple/

COPY requirements.txt .
COPY scripts/prepare-runtime-requirements.py /usr/local/bin/prepare-runtime-requirements.py
RUN set -eux; \
    runtime_project="molecules-workspace-runtime"; \
    rm -rf /tmp/molecule-runtime; \
    mkdir -p /tmp/molecule-runtime; \
    runtime_requirement="$(python3 /usr/local/bin/prepare-runtime-requirements.py \
      requirements.txt /tmp/template-requirements.txt "${RUNTIME_VERSION}")"; \
    case "${runtime_requirement}" in "${runtime_project}"*) ;; *) exit 1 ;; esac; \
    pip download --isolated --only-binary=:all: --no-deps \
      --index-url "$MOLECULE_RUNTIME_INDEX" \
      --dest /tmp/molecule-runtime \
      "${runtime_requirement}"; \
    runtime_wheel_count="$(find /tmp/molecule-runtime -maxdepth 1 -type f -name 'molecules_workspace_runtime-*.whl' | wc -l)"; \
    test "${runtime_wheel_count}" -eq 1; \
    runtime_wheel="$(find /tmp/molecule-runtime -maxdepth 1 -type f -name 'molecules_workspace_runtime-*.whl')"; \
    pip install --isolated --no-cache-dir \
      "${runtime_wheel}" -r /tmp/template-requirements.txt; \
    rm -rf /tmp/molecule-runtime /tmp/template-requirements.txt

# --- Pre-bake the management-MCP server (base-runtime helper; task #54) ---
# The kind=platform concierge launches `npx --prefer-offline @molecule-ai/mcp-server@<PIN>`
# in a HARD-deadline enumeration spawn at boot; without a warm cache it cold-pulls
# -> ETARGET / CF-WAF throttle -> #1027 fail-close (launch-side of RCA #2970). The bake
# LOGIC + the pinned version now live ONCE in the base runtime (molecule_runtime, pinned
# to the SDK contract management_mcp_server block) — this template DELEGATES to the shared
# helper instead of carrying its own bake + ARG + Guard-D lint (ADR-004: SDK contract ->
# base-runtime default -> per-adapter override-if-needed; no per-template fork). The pin
# lockstep is now enforced ONCE in the runtime (constants == contract), so the per-template
# Guard-D gate is removed. openclaw ships node globally on PATH, so no
# MOLECULE_PREBAKE_NODE_BIN override. The helper's build-time OFFLINE self-check fails the
# image if the bake is broken.
USER agent
RUN bash "$(python3 -c 'import molecule_runtime, os; print(os.path.dirname(molecule_runtime.__file__))')/scripts/prebake-mgmt-mcp.sh"
USER root

# Make the pre-baked mgmt-MCP npm cache + scoped-registry config HOME-INDEPENDENT.
# Per contracts/mcp-plugin-delivery.contract.json `home_independent`: the
# kind=platform concierge launches `npx --prefer-offline @molecule-ai/mcp-server`
# under a HOME that is NOT guaranteed to be the agent home (observed HOME=/root in
# the provisioner), so a per-user ${HOME}/.npmrc / ${HOME}/.npm is MISSED — npx
# then falls through to the PUBLIC registry -> ETARGET on the private @molecule-ai
# scope -> #1027 "management MCP FAILED TO LAUNCH" fail-close, EVEN THOUGH the
# version is correctly baked. Reproduced: openclaw peer-visibility boot is ~6x
# slower than hermes and flaps to `failed` under concurrent CF-WAF throttle;
# hermes is unaffected because its runtime propagates the launch env. The prebake
# helper can't `npm config set --global` (EACCES as the non-root agent), so pin
# the two launch vars as container ENV instead — inherited by the mgmt-MCP spawn
# regardless of HOME, pointing npx at the baked cache + scoped-registry npmrc.
ENV npm_config_cache=/home/agent/.npm \
    NPM_CONFIG_USERCONFIG=/home/agent/.npmrc


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
