#!/bin/sh
# Source persistent workspace secrets BEFORE anything else that might need them.
# /configs is volume-mounted from the host so this survives container restart.
if [ -f /configs/secrets.d/load.sh ]; then
  . /configs/secrets.d/load.sh
fi

# Drop privileges to the agent user before exec'ing molecule-runtime.
#
# Why this exists (T4 + list_peers atomic-close — RFC internal#456 §9-11)
# ----------------------------------------------------------------------
# Previously this template had NO entrypoint wrapper: the Dockerfile's
# `ENTRYPOINT ["molecule-runtime"]` ran the runtime as ROOT. With the
# runtime as root, `list_peers` (via the molecule A2A MCP sidecar) worked
# only "by accident (root==root)": molecule_runtime.platform_auth reads
# the workspace bearer token from `/configs/.auth_token` with a plain
# `path.read_text()` — no uid check — so root could read it regardless of
# which uid the platform provisioner created the file as.
#
# The T4 escalation leg (Dockerfile: sudo NOPASSWD + docker group +
# nsenter) requires the agent process to run as the uid-1000 `agent`
# user (mirroring template-claude-code's proven, live-verified pattern).
# Dropping to uid-1000 WITHOUT making the token agent-readable would
# regress `list_peers` to the exact Hermes list_peers-401 class: the
# uid-1000 runtime could no longer read a root-owned `.auth_token`, the
# A2A MCP sidecar would fail platform auth, and peer-discovery would
# silently fall back to OpenClaw's native `sessions_list`.
#
# Therefore T4 and platform-abilities MUST close ATOMICALLY: this
# entrypoint runs as root, chowns /configs (and thus
# /configs/.auth_token) to `agent:agent`, THEN re-execs the runtime via
# `gosu agent` so the runtime is uid-1000 AND can still read the token.
# Both halves ship in the same image revision; the t4-conformance CI
# gate asserts both on a live container (host-root reach AND
# /configs/.auth_token owner_uid==1000) and fails closed.
#
# Pattern matches template-claude-code/entrypoint.sh (the proven T4
# reference, ECR sha256:e1ae9795… / git bbc2dae): fix volume ownership
# as root, then re-exec via gosu as agent (uid 1000).

# Boot-context snapshot — emitted on EVERY container start, including
# every restart of a crash-loop. Logs NAMES of auth-relevant env vars,
# never VALUES. Fires twice (once as root pre-gosu, once as agent
# post-gosu) so an operator can see whether a value or the token
# ownership survived the privilege drop.
log_boot_context() {
    echo "----- entrypoint boot $(date -u +%Y-%m-%dT%H:%M:%SZ) -----"
    echo "uid=$(id -u) gid=$(id -g) user=$(id -un 2>/dev/null || echo unknown)"
    echo "hostname=$(hostname) workspace_id=${WORKSPACE_ID:-<unset>}"
    echo "platform_url=${PLATFORM_URL:-<unset>}"
    echo "configs_dir: $(ls -ld /configs 2>/dev/null || echo MISSING)"
    echo "configs_contents: $(ls /configs 2>/dev/null | tr '\n' ' ' || echo MISSING)"
    if [ -e /configs/.auth_token ]; then
        echo "auth_token: $(ls -l /configs/.auth_token 2>/dev/null) owner_uid=$(stat -c '%u' /configs/.auth_token 2>/dev/null)"
    else
        echo "auth_token: <not yet issued>"
    fi
    echo "workspace_dir: $(ls -ld /workspace 2>/dev/null || echo MISSING)"
    for var in ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN ANTHROPIC_BASE_URL MINIMAX_API_KEY GLM_API_KEY KIMI_API_KEY DEEPSEEK_API_KEY; do
        eval "val=\$$var"
        if [ -n "$val" ]; then
            echo "env $var=set"
        else
            echo "env $var=unset"
        fi
    done
    echo "------------------------------------------------"
}
log_boot_context

if [ "$(id -u)" = "0" ]; then
    # T4 atomic-co-sequencing contract (RFC internal#456 §10): the T4
    # escalation leg (sudo NOPASSWD + docker group + nsenter, baked in
    # the Dockerfile) is ADDITIVE. The agent runs uid-1000 and
    # /configs/.auth_token MUST remain agent-readable — escalation must
    # NOT regress the Hermes list_peers-401 token-ownership class. This
    # chown -R is the agent-ownership half of that contract; the
    # t4-conformance gate asserts owner_uid==1000 on the running
    # container alongside the host-root-reach assertion.
    #
    # /configs is created by Docker as root; the uid-1000 agent needs
    # read access to /configs/.auth_token (platform bearer token, the
    # list_peers auth) and write access for plugin installs, memory
    # writes, and .auth_token rotation.
    chown -R agent:agent /configs 2>/dev/null
    # /workspace handling — only chown when the contents are root-owned.
    chown agent:agent /workspace 2>/dev/null || true
    if [ -d /workspace ]; then
        first_entry=$(find /workspace -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)
        if [ -n "$first_entry" ] && [ "$(stat -c '%u' "$first_entry" 2>/dev/null)" = "0" ]; then
            chown -R agent:agent /workspace 2>/dev/null
        fi
    fi
    # OpenClaw writes its gateway state + MCP registration under the
    # agent home (`openclaw mcp set molecule` persists to ~/.openclaw).
    # Pre-create + chown so the uid-1000 agent never EPERMs writing the
    # MCP server registration that wires list_peers into OpenClaw's tool
    # loop (the platform-abilities half of the atomic close).
    mkdir -p /home/agent/.openclaw 2>/dev/null || true
    chown -R agent:agent /home/agent/.openclaw 2>/dev/null || true

    exec gosu agent "$0" "$@"
fi

# Now running as agent (uid 1000)
#
# Third-party provider routing is handled by adapter.py at boot — it
# reads the `providers:` registry from /configs/config.yaml and sets
# ANTHROPIC_BASE_URL based on the picked MODEL. Operator-set
# ANTHROPIC_BASE_URL still wins as the escape hatch for regional
# endpoints.

exec molecule-runtime "$@"
