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

    # openclaw mgmt-MCP fix (RFC#2843 #32, ported from template-claude-code):
    # install the workspace's DECLARED plugins (the DB desired-set, passed as
    # MOLECULE_DECLARED_PLUGINS — comma-separated gitea:// sources) into
    # /configs/plugins BEFORE dropping to agent + exec'ing the runtime.
    #
    # WHY openclaw NEEDS THIS: the openclaw adapter setup() loads plugins from
    # /configs/plugins (molecule_runtime.plugins.load_plugins, filesystem only)
    # and wires each declared MCP into ~/.openclaw/openclaw.json via
    # register_mcp_server_hook. The privileged molecule-platform-mcp plugin
    # (the create_workspace/provision_workspace org-admin MCP a CONCIERGE needs)
    # is delivered as a declared gitea:// source — but nothing materialized it
    # into the filesystem for openclaw, so load_plugins found nothing and the
    # concierge booted WITHOUT the management MCP, leaving mcp_server_present=
    # false and RCA#2970 fail-closing it to `failed` (A2A 503). claude-code does
    # this same fetch; openclaw was simply missing the block. Fail-soft: a
    # fetch/extract failure logs and continues; never blocks boot.
    if [ -n "${MOLECULE_DECLARED_PLUGINS:-}" ]; then
        _plg_base="${MOLECULE_GITEA_BASE_URL:-https://git.moleculesai.app}"
        _plg_base="${_plg_base%/}"
        rm -rf /configs/plugins 2>/dev/null
        mkdir -p /configs/plugins
        _plg_old_ifs="$IFS"
        IFS=','
        for _plg_src in $MOLECULE_DECLARED_PLUGINS; do
            IFS="$_plg_old_ifs"
            _plg_src="$(printf '%s' "$_plg_src" | tr -d '[:space:]')"
            if [ -z "$_plg_src" ]; then IFS=','; continue; fi
            case "$_plg_src" in
                gitea://*) : ;;
                *) echo "[plugins] skip unsupported source: $_plg_src"; IFS=','; continue ;;
            esac
            _plg_spec="${_plg_src#gitea://}"
            _plg_ref="main"
            case "$_plg_spec" in *"#"*) _plg_ref="${_plg_spec##*#}"; _plg_spec="${_plg_spec%%#*}" ;; esac
            _plg_owner="${_plg_spec%%/*}"
            _plg_rest="${_plg_spec#*/}"
            _plg_repo="${_plg_rest%%/*}"
            _plg_sub="${_plg_rest#*/}"
            [ "$_plg_sub" = "$_plg_rest" ] && _plg_sub=""
            if [ -n "$_plg_sub" ]; then _plg_name="${_plg_sub##*/}"; else _plg_name="$_plg_repo"; fi
            if [ -z "$_plg_owner" ] || [ -z "$_plg_repo" ] || [ -z "$_plg_name" ]; then
                echo "[plugins] bad source: $_plg_src"; IFS=','; continue
            fi
            _plg_td="$(mktemp -d)"
            _plg_url="${_plg_base}/api/v1/repos/${_plg_owner}/${_plg_repo}/archive/${_plg_ref}.tar.gz"
            if [ -n "${MOLECULE_TEMPLATE_REPO_TOKEN:-}" ]; then
                curl -fsSL --retry 3 --max-time 120 -H "Authorization: token ${MOLECULE_TEMPLATE_REPO_TOKEN}" "$_plg_url" -o "$_plg_td/a.tgz"
            else
                curl -fsSL --retry 3 --max-time 120 "$_plg_url" -o "$_plg_td/a.tgz"
            fi
            if [ -s "$_plg_td/a.tgz" ] && tar -xzf "$_plg_td/a.tgz" -C "$_plg_td" 2>/dev/null; then
                _plg_top="$(find "$_plg_td" -mindepth 1 -maxdepth 1 -type d | head -n1)"
                _plg_dir="$_plg_top"
                [ -n "$_plg_sub" ] && _plg_dir="$_plg_top/$_plg_sub"
                if [ -d "$_plg_dir" ]; then
                    mkdir -p "/configs/plugins/$_plg_name"
                    cp -a "$_plg_dir/." "/configs/plugins/$_plg_name/" 2>/dev/null \
                        && echo "[plugins] installed $_plg_name <- $_plg_src" \
                        || echo "[plugins] copy failed: $_plg_src"
                else
                    echo "[plugins] subpath not in archive: $_plg_sub ($_plg_src)"
                fi
            else
                echo "[plugins] fetch/extract failed: $_plg_src"
            fi
            rm -rf "$_plg_td"
            IFS=','
        done
        IFS="$_plg_old_ifs"
        chown -R agent:agent /configs/plugins 2>/dev/null || true
    fi
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

# --- npm auth for the private @molecule-ai scope (gitea read:package) -----
# The org-management MCP a CONCIERGE declares (molecule-platform-mcp) is launched
# at runtime via `npx -y @molecule-ai/mcp-server`. That package is PRIVATE on the
# gitea npm registry, so without a scoped `_authToken` npx 404s/ETARGETs it and
# the MCP never starts — the concierge boots without create_workspace (the
# fleet-wide "degraded concierge" root cause, runtime #176). OpenClaw itself is
# a PUBLIC npm package (installed in adapter.py), so this auth is needed ONLY for
# the private molecule-platform-mcp at runtime.
#
# Ported from template-codex/start.sh (which mirrors runtime #176
# `npm_auth.install_npm_gitea_auth()`: ~/.npmrc = scope→gitea registry +
# `_authToken` from the SAME gitea token, SSOT). We prefer the runtime helper
# when the installed base provides it; otherwise write ~/.npmrc directly so an
# older base image still gets a launchable MCP. The token comes from
# MOLECULE_TEMPLATE_REPO_TOKEN (the read-only gitea PAT widened to read:package,
# runtime #176 prereq); never echoed. We are already the uid-1000 agent here
# (post-gosu), so ~/.npmrc resolves to /home/agent/.npmrc, read by the npx the
# runtime invokes.
NPM_GITEA_BASE="${MOLECULE_GITEA_BASE_URL:-https://git.moleculesai.app}"
NPM_GITEA_TOKEN="${MOLECULE_TEMPLATE_REPO_TOKEN:-${GITEA_TOKEN:-}}"
if [ -n "${NPM_GITEA_TOKEN}" ]; then
  RUNTIME_PY=""
  for cand in /opt/molecule-venv/bin/python3 /opt/molecule-venv/bin/python python3 python; do
    if command -v "$cand" >/dev/null 2>&1; then RUNTIME_PY="$cand"; break; fi
  done
  _wrote_npmrc=""
  if [ -n "$RUNTIME_PY" ] && \
     "$RUNTIME_PY" -c "import molecule_runtime.npm_auth" >/dev/null 2>&1; then
    # Preferred: the runtime SSOT helper (runtime #176). We are already agent.
    if MOLECULE_GITEA_BASE_URL="$NPM_GITEA_BASE" \
       MOLECULE_TEMPLATE_REPO_TOKEN="$NPM_GITEA_TOKEN" \
       "$RUNTIME_PY" -c \
       "import molecule_runtime.npm_auth as n; n.install_npm_gitea_auth()" \
       >/dev/null 2>&1; then
      _wrote_npmrc="runtime-helper"
    fi
  fi
  if [ -z "$_wrote_npmrc" ]; then
    # Fallback: write ~/.npmrc directly. Gitea npm registry path is
    # /api/packages/<owner>/npm/. The @molecule-ai scope → owner molecule-ai.
    NPM_REGISTRY="${NPM_GITEA_BASE%/}/api/packages/molecule-ai/npm/"
    # Strip scheme for the //host/path:_authToken key gitea/npm expects.
    NPM_REGISTRY_NOSCHEME="${NPM_REGISTRY#https:}"
    NPM_REGISTRY_NOSCHEME="${NPM_REGISTRY_NOSCHEME#http:}"
    NPMRC="${HOME:-/home/agent}/.npmrc"
    {
      printf '@molecule-ai:registry=%s\n' "$NPM_REGISTRY"
      printf '%s:_authToken=%s\n' "$NPM_REGISTRY_NOSCHEME" "$NPM_GITEA_TOKEN"
    } > "$NPMRC"
    chmod 0600 "$NPMRC" 2>/dev/null || true
    _wrote_npmrc="direct"
  fi
  echo "[entrypoint.sh] npm gitea auth configured (@molecule-ai scope, ${_wrote_npmrc}); npx @molecule-ai/mcp-server can resolve the private package"
else
  echo "[entrypoint.sh] WARN: no MOLECULE_TEMPLATE_REPO_TOKEN/GITEA_TOKEN — npx @molecule-ai/mcp-server (management MCP) will ETARGET the private package" >&2
fi

exec molecule-runtime "$@"
