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

# Generic GIT_ASKPASS helper. Reads HTTPS Basic-Auth credentials from
# env vars (GIT_HTTP_USERNAME / GIT_HTTP_PASSWORD, with GITEA_USER /
# GITEA_TOKEN as fallback) and emits them on the git credential-prompt
# protocol, so container-side `git` can authenticate to any private
# HTTPS remote without on-disk .gitconfig / .git-credentials mutation.
# Installed as /usr/local/bin/molecule-askpass — the platform-side
# provisioner sets GIT_ASKPASS to that path. Script body contains no
# hostnames or vendor literals; the deployer decides which remote the
# credentials apply to by virtue of populating those env vars.
COPY scripts/git-askpass.sh /usr/local/bin/molecule-askpass
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
