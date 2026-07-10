"""OpenClaw adapter — bridges OpenClaw's Node.js gateway with our A2A protocol.

OpenClaw is a Node.js agent runtime with its own gateway (port 18789).
This adapter:
1. Installs OpenClaw CLI (npm) and missing deps in the container
2. Runs non-interactive onboard with the configured model provider
3. Copies workspace files (SOUL.md, BOOTSTRAP.md, etc.) to OpenClaw's workspace dir
4. Starts the OpenClaw gateway as a background process
5. Proxies A2A messages via `openclaw agent --json` CLI subprocess
"""

import asyncio
import json
import logging
import os
import shutil
import subprocess

from molecule_runtime.adapters.base import BaseAdapter, AdapterConfig
from a2a.server.agent_execution import AgentExecutor

# NOTE: the message-extraction / task-brief / attachment / vision helpers
# (extract_message_text, brief_task, set_current_task, extract_attached_files,
# append_image_descriptions) are DELIBERATELY not imported here anymore. They
# were only used by this template's old hand-rolled ``execute()`` override, which
# has been removed — the shared ``SubprocessA2AExecutor`` base now owns the whole
# ``execute()`` body (message extraction, image enrichment, session-id, heartbeat)
# so this adapter stays a THIN subclass and does not duplicate the base's imports.

# Shared subprocess-executor base (tenant-agent BUG 3): the session CONTRACT —
# a STABLE WORKSPACE_ID-keyed session id so the runtime's native session RESUMES
# across turns — lives ONCE in the SSOT runtime SDK so every subprocess runtime
# inherits it. Continuity is that native session, NOT a force-injected transcript:
# the base passes ONLY the current user message to run_agent (no metadata.history
# is prepended; older history is retrieved only if the agent chooses to call a
# platform-workspace MCP tool). OpenClawA2AExecutor below is a thin subclass that
# provides ONLY the openclaw shell-out (run_agent) + MEDIA adornment; it does NOT
# override execute() and does NOT inject history.
from molecule_runtime.subprocess_executor import SubprocessA2AExecutor

# Turn-lease liveness (MUST-FIX 1) is provided by the mailbox-kernel runtime.
# GUARDED: the template's requirements pin molecule-ai-workspace-runtime>=0.3.11,
# and wheels predating the mailbox kernel do not ship molecule_runtime.turn_lease
# — so a hard `from molecule_runtime import turn_lease` would break every turn on
# an older runtime. Import it optionally; _lease_touch/_lease_reset below no-op
# when it is absent (or when the kernel is off / no lease installed), so the
# adapter runs unchanged on old runtimes and renews the lease on new ones.
try:
    from molecule_runtime import turn_lease as _turn_lease
except Exception:  # pragma: no cover - older runtime wheel without the mailbox kernel
    _turn_lease = None


def _lease_reset() -> None:
    """Arm the process-global turn lease at turn start. No-op when the runtime
    predates the mailbox kernel (turn_lease absent) or no lease is installed."""
    if _turn_lease is not None:
        _turn_lease.reset_current()


def _lease_touch() -> None:
    """Renew the process-global turn lease (turn-lease source D). No-op when the
    runtime predates the mailbox kernel or no lease is installed."""
    if _turn_lease is not None:
        _turn_lease.touch_current()


logger = logging.getLogger(__name__)

# Providers supported by this adapter; maps prefix → (auth_env_vars, default_base_url).
OPENCLAW_PROVIDERS = {
    "openai":     (("OPENAI_API_KEY",),                     "https://api.openai.com/v1"),
    "groq":       (("GROQ_API_KEY",),                       "https://api.groq.com/openai/v1"),
    "openrouter": (("OPENROUTER_API_KEY",),                 "https://openrouter.ai/api/v1"),
    "qianfan":    (("QIANFAN_API_KEY", "AISTUDIO_API_KEY"), "https://qianfan.baidubce.com/v2"),
    "minimax":    (("MINIMAX_API_KEY",),                    "https://api.minimax.io/v1"),
    "moonshot":   (("KIMI_API_KEY",),                       "https://api.moonshot.ai/v1"),
}

def _openclaw_workspace_dir() -> str:
    """Resolve the workspace dir OpenClaw's gateway ACTUALLY reads SOUL.md from.

    Mirrors openclaw's own ``resolveDefaultAgentWorkspaceDir`` (verified against
    openclaw 2026.6.11, dist/config-utils): the default agent's workspace is
    ``~/.openclaw/workspace``, partitioned to ``~/.openclaw/workspace-<profile>``
    ONLY when ``OPENCLAW_PROFILE`` is set to a non-``default`` value. It is NOT
    suffixed with the agent id for the default agent.

    The prior literal ``~/.openclaw/workspace-dev/main`` was wrong on BOTH axes:
    it assumed the gateway's ``--dev`` flag implies ``OPENCLAW_PROFILE=dev`` (it
    does not — ``--dev`` is a gateway launch mode, orthogonal to the workspace
    profile) AND it appended a ``/main`` agent-id segment the resolver never adds
    for the default agent. The materialized SOUL.md therefore landed in a dir the
    gateway never read, so a fresh openclaw concierge ran the STOCK OpenClaw
    SOUL.md and self-identified generically. Resolved per-call (like
    ``_openclaw_config_path``) so it tracks $HOME / OPENCLAW_PROFILE at use time.
    """
    profile = (os.environ.get("OPENCLAW_PROFILE") or "").strip().lower()
    if profile and profile != "default":
        return os.path.expanduser(f"~/.openclaw/workspace-{profile}")
    return os.path.expanduser("~/.openclaw/workspace")


# The workspace dir OpenClaw reads (SOUL.md et al.). Resolved at import from the
# same rule the gateway uses; see _openclaw_workspace_dir for the fix rationale.
OPENCLAW_WORKSPACE = _openclaw_workspace_dir()
OPENCLAW_PORT = 18789
# Port for the molecule A2A MCP server (HTTP transport). HTTP is used
# instead of stdio because a2a_mcp_server's stdio main() blocks on a
# fixed-size stdin.read(65536) that never returns for a small
# keep-stdin-open MCP client (OpenClaw's bundle-mcp), tripping a 30s
# handshake timeout. The HTTP transport (also used by Hermes) is unaffected.
OPENCLAW_MCP_HTTP_PORT = 9100

# Known missing optional deps in OpenClaw's npm package
OPENCLAW_MISSING_DEPS = ["@buape/carbon", "@larksuiteoapi/node-sdk", "@slack/web-api", "grammy"]

# OpenClaw-native MCP config — ~/.openclaw/openclaw.json  mcp.servers.<name>.
# This is the file `openclaw mcp set <name> '<json>'` mutates (verified against
# openclaw@2026.5.7): the stdio descriptor ({command, args?, env?}) nested under
# mcp.servers.<name>. The adapter renders this file DIRECTLY (see
# ``_render_openclaw_mcp`` / ``register_mcp_server_hook``) rather than dispatching
# to the runtime base's ``render_for_runtime`` — the openclaw branch of which is
# a deliberate fail-loud ``NotImplementedError`` stub on runtime versions that
# predate the openclaw renderer. Owning the format here keeps the management-MCP
# install path working on every pinned runtime and makes the present-reader agree
# with what we wrote (the RCA#2970 gate's "is the management MCP wired?" probe).
OPENCLAW_MCP_PARENT = "mcp"
OPENCLAW_MCP_SERVERS = "servers"


def _openclaw_config_path() -> str:
    """Absolute path to ~/.openclaw/openclaw.json, resolved at CALL time.

    Resolved per-call (not frozen at import) so it tracks $HOME — matching the
    runtime's own ``_openclaw_path`` and the rest of this adapter, which all read
    ``os.path.expanduser("~/.openclaw/...")`` at use time."""
    return os.path.expanduser("~/.openclaw/openclaw.json")


# Canonical name of the privileged org-management MCP, sourced from the runtime
# so the adapter's present-reader matches the exact server name the RCA#2970 gate
# probes for. Falls back to the literal on older runtimes lacking the symbol.
try:
    from molecule_runtime.platform_agent_identity import (
        MANAGEMENT_MCP_NAME as MANAGEMENT_MCP_NAME,
    )
except Exception:  # pragma: no cover — older runtime without the constant
    MANAGEMENT_MCP_NAME = "molecule-platform"


def _load_openclaw_config(path: str) -> dict:
    """Read ~/.openclaw/openclaw.json as a dict; {} on missing/malformed."""
    try:
        with open(path) as _f:
            data = json.load(_f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _render_openclaw_mcp(config_path: str, name: str, spec: dict) -> None:
    """Additively merge ``name -> spec`` into ~/.openclaw/openclaw.json's
    ``mcp.servers`` map. Idempotent; preserves every other key + server.

    Produces the exact on-disk shape ``openclaw mcp set <name> '<json>'`` writes
    (the stdio descriptor under ``mcp.servers.<name>``). Written directly rather
    than shelling out so it works before the openclaw binary is on PATH and is
    pure/testable — mirrors the runtime's own ``render_openclaw_config``.
    """
    os.makedirs(os.path.dirname(config_path), exist_ok=True)
    data = _load_openclaw_config(config_path)
    mcp = data.get(OPENCLAW_MCP_PARENT)
    if not isinstance(mcp, dict):
        mcp = {}
    servers = mcp.get(OPENCLAW_MCP_SERVERS)
    if not isinstance(servers, dict):
        servers = {}
    servers[name] = dict(spec)
    mcp[OPENCLAW_MCP_SERVERS] = servers
    data[OPENCLAW_MCP_PARENT] = mcp
    with open(config_path, "w") as _f:
        _f.write(json.dumps(data, indent=2) + "\n")


def _openclaw_mcp_present(config_path: str, name: str) -> bool:
    """True when ~/.openclaw/openclaw.json declares ``mcp.servers.<name>``.

    Fail-closed by construction: a missing, unreadable, malformed, or
    structurally-unexpected config yields False, so a genuinely MCP-less openclaw
    concierge stays fail-closed (degraded) at the RCA#2970 gate."""
    data = _load_openclaw_config(config_path)
    mcp = data.get(OPENCLAW_MCP_PARENT)
    if not isinstance(mcp, dict):
        return False
    servers = mcp.get(OPENCLAW_MCP_SERVERS)
    return isinstance(servers, dict) and name in servers


def _read_openclaw_mcp_servers(config_path: str) -> dict:
    """Read the ``mcp.servers`` {name: spec} map from ~/.openclaw/openclaw.json.

    The INVERSE of ``_render_openclaw_mcp`` (ADR-004 socket ``read_mcp_servers``):
    it reuses the SAME native path + parser as ``_openclaw_mcp_present`` so a
    server the renderer writes is byte-for-byte the one enumerate spawns. Moved
    into the adapter (faithful copy of the runtime engine's
    ``mcp_render._read_openclaw_mcp_servers``) so ``enumerate_loaded_mcp_tools``
    round-trips openclaw's OWN native config without dispatching through the
    engine's ``read_mcp_servers_for`` switch. Fail-closed: a missing / unreadable
    / malformed / structurally-unexpected config yields ``{}`` (never crashes the
    enumerate path)."""
    data = _load_openclaw_config(config_path)
    mcp = data.get(OPENCLAW_MCP_PARENT)
    if not isinstance(mcp, dict):
        return {}
    servers = mcp.get(OPENCLAW_MCP_SERVERS)
    return (
        {k: v for k, v in servers.items() if isinstance(v, dict)}
        if isinstance(servers, dict)
        else {}
    )


# OpenClaw-native identity (persona) convention. OpenClaw's gateway reads identity
# from SOUL.md in its workspace, populated by copying top-level ``/configs/*.md``
# at setup — so writing ``/configs/SOUL.md`` makes the canonical persona the
# model's actual identity, overlaying the baked placeholder SOUL.md. The baked
# BOOTSTRAP.md / AGENTS.md placeholders are cleared (overwritten with a one-line
# pointer) so their generic boilerplate can't compete with the strong materialized
# identity. Mirrors the runtime engine's persona_render OPENCLAW_PERSONA_FILE /
# OPENCLAW_CLEARED_FILES (ADR-004 socket ``materialize_persona``).
OPENCLAW_PERSONA_FILE = "SOUL.md"
OPENCLAW_CLEARED_FILES = ("BOOTSTRAP.md", "AGENTS.md")


def _write_persona_file(target, persona: str) -> None:
    """Write ``persona`` to ``target`` (parents created), trailing newline.

    Faithful copy of persona_render._write_persona_file so the byte-shape of the
    materialized SOUL.md is identical to the engine's."""
    from pathlib import Path as _Path

    target = _Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    body = persona if persona.endswith("\n") else persona + "\n"
    target.write_text(body, encoding="utf-8")


def _materialize_openclaw_persona(config_path, persona: str):
    """OpenClaw — write ``persona`` to ``<configs>/SOUL.md`` and CLEAR the
    ``BOOTSTRAP.md`` / ``AGENTS.md`` placeholders. Returns the SOUL.md path.

    Byte-identical to the runtime engine's
    ``persona_render.materialize_openclaw_persona`` (moved into the adapter per
    ADR-004): writes the top-level ``/configs/SOUL.md`` so the canonical persona
    becomes the model's actual identity overlaying the baked placeholder SOUL.md,
    and overwrites the baked BOOTSTRAP.md / AGENTS.md with a neutral one-liner so
    no competing identity boilerplate loads."""
    from pathlib import Path as _Path

    base = _Path(config_path)
    target = base / OPENCLAW_PERSONA_FILE
    _write_persona_file(target, persona)
    for cleared in OPENCLAW_CLEARED_FILES:
        stub = base / cleared
        _write_persona_file(
            stub,
            f"# {cleared[:-3]}\n\n"
            "(Cleared by persona materialization — this workspace's identity and "
            "role are defined in SOUL.md; discover and delegate to peers via the "
            "`molecule` MCP tools.)",
        )
    return target

# This template's declared default model — mirrors config.yaml's `model:`
# field. Two hard constraints (both required; routability alone is NOT
# enough — that gap is the second root cause this default fixes):
#
#  1. Its provider prefix MUST exist in OPENCLAW_PROVIDERS above so the
#     adapter can structurally route it (PR#18's invariant).
#  2. Its provider's required env key MUST be the credential a *fresh*
#     openclaw workspace genuinely receives. The earlier
#     `openai:gpt-4.1-mini` satisfied (1) but NOT (2): a fresh prod
#     openclaw workspace is provisioned with a `sk-cp-*`
#     ``MINIMAX_API_KEY`` (the seeded `custom-api-minimaxi-com`
#     provider), NOT an ``OPENAI_API_KEY``. So coerce_servable_model
#     correctly rewrote the unroutable ``anthropic:`` generic default to
#     ``openai:gpt-4.1-mini``, but resolve_provider_routing then raised
#     ``No API key found for provider 'openai' (checked: OPENAI_API_KEY)``
#     and aborted setup() before the platform MCP was ever registered —
#     bricking list_peers on every default-config provision.
#
# THE single shared platform-default model is the Infisical SSOT
# ``MOLECULE_LLM_DEFAULT_MODEL`` (/shared/controlplane/llm), read at import. The
# literal below is only the LAST-RESORT fallback when that env is unset, and it
# matches the CP/core ``platformDefaultModelFallback`` (minimax/MiniMax-M2.7).
#
# Both forms below now derive from the SAME shared value, removing the prior
# divergence where the BYOK default was MiniMax but the platform default was Kimi
# (moonshot/kimi-k2.6) — a fresh platform_managed openclaw and a fresh BYOK
# openclaw now resolve the SAME default, never two different vendors. ``minimax``
# is in OPENCLAW_PROVIDERS and ``MINIMAX_API_KEY`` is the credential a fresh
# openclaw workspace actually has (the seeded ``custom-api-minimaxi-com`` sk-cp-*
# provider; the step-2b routing override steers it to MiniMax's Anthropic-compat
# endpoint), so the colon-form default is key-consistent on first boot.
def _ssot_default_model(sep: str) -> str:
    raw = (os.environ.get("MOLECULE_LLM_DEFAULT_MODEL") or "").strip()
    if not raw:
        raw = "minimax/MiniMax-M2.7"  # shared fallback == CP/core platformDefaultModelFallback
    # openclaw's BYOK registry namespaces as ``<provider>:<model>`` (colon); the
    # platform proxy uses ``<vendor>/<model>`` (slash). Normalise to the form the
    # caller needs WITHOUT changing the model identity.
    return raw.replace("/", ":", 1) if sep == ":" else raw.replace(":", "/", 1)


# BYOK colon-form default for the per-vendor registry (coerce_servable_model).
OPENCLAW_DEFAULT_MODEL = _ssot_default_model(":")

# Platform-managed slash-form default for the proxy (resolve_platform_routing).
OPENCLAW_PLATFORM_DEFAULT_MODEL = _ssot_default_model("/")


def _parse_tools_list_body(body: str) -> list[str]:
    """Extract tool names from a JSON-RPC ``tools/list`` response body.

    Tolerant of the three framings an MCP server may use over stdio/http:
    a single JSON object, newline-delimited JSON (one object per line, the
    stdio default), and SSE (``data: {…}`` lines). Returns the names under
    ``result.tools[].name`` from whichever frame carries them; [] if none.
    The core#3082 producer keys on these names (``mcp__<server>__<name>``).
    """
    candidates: list[str] = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("data:"):
            line = line[len("data:"):].strip()
        if not (line.startswith("{") or line.startswith("[")):
            continue
        candidates.append(line)
    # Also try the whole body as one JSON document (single-object http reply).
    stripped = body.strip()
    if stripped and stripped not in candidates:
        candidates.append(stripped)

    for cand in candidates:
        try:
            data = json.loads(cand)
        except ValueError:
            continue
        result = data.get("result") if isinstance(data, dict) else None
        tools = result.get("tools") if isinstance(result, dict) else None
        if isinstance(tools, list):
            names = [
                t["name"] for t in tools
                if isinstance(t, dict) and isinstance(t.get("name"), str) and t.get("name")
            ]
            if names:
                return names
    return []


def resolve_platform_routing(model_str, env):
    """When the workspace's resolved provider is ``platform``, route through
    the Molecule platform proxy's OpenAI-compat surface, bypassing the
    per-vendor colon registry (resolve_provider_routing) entirely — for the
    platform arm the tenant has no BYOK key (the workspace-server strips them),
    so the registry path would raise "No API key found".

    Provider selection is flag-free — ``platform`` is selected the same way any
    other provider is, by the resolved provider, NOT by a billing-mode env.

    ``MOLECULE_RESOLVED_PROVIDER`` is the SSOT signal and the TOP-PRECEDENCE
    explicit provider: core's provisioner resolves the provider ONCE (Go
    ``manifest.DeriveProvider``) and publishes the registry arm name here for
    every layer to READ, never re-derive. When it is set it is authoritative —
    ``platform`` is selected iff its value is exactly ``platform``; any other
    (byok) arm means NOT platform and the model namespace must NOT re-promote it
    to platform. Only when the SSOT signal is ABSENT do we fall back to the
    legacy signals (back-compat for old provisioners):
      * ``LLM_PROVIDER``/``MODEL_PROVIDER`` env == ``platform`` (core injected
        ``LLM_PROVIDER=platform`` for platform-routed workspaces), or
      * a ``platform/`` / ``platform:`` model namespace marker.

    Returns (api_key, provider_url, model, compatibility) or None when the
    resolved provider is not ``platform``. Raises RuntimeError if it IS
    platform but the proxy env is unconfigured (fail closed rather than fall
    through to a keyless registry route).

    The model id is sent verbatim (a leading "platform/" / "platform:"
    namespace marker is stripped); the proxy keys on the vendor prefix
    (moonshot/..., minimax/..., anthropic/..., openai/...). Mirrors the proven
    smoke (molecule-controlplane scripts/e2e-llm-kimi-smoke.sh).
    """
    model = (model_str or "").strip()
    resolved = (env.get("MOLECULE_RESOLVED_PROVIDER") or "").strip().lower()
    if resolved:
        # SSOT signal present: it is authoritative (top precedence). Route
        # platform iff the resolved arm name is exactly ``platform``; any other
        # arm is BYOK and must NOT be re-derived from LLM_PROVIDER/MODEL_PROVIDER
        # or the model namespace.
        is_platform = (resolved == "platform")
    else:
        # Back-compat: no SSOT signal — fall back to the legacy LLM_PROVIDER/
        # MODEL_PROVIDER env or the ``platform/`` / ``platform:`` model marker.
        env_provider = (env.get("LLM_PROVIDER") or env.get("MODEL_PROVIDER") or "").strip().lower()
        is_platform = (
            env_provider == "platform"
            or model.startswith("platform/")
            or model.startswith("platform:")
        )
    if not is_platform:
        return None
    base = env.get("MOLECULE_LLM_BASE_URL") or env.get("OPENAI_BASE_URL")
    token = env.get("MOLECULE_LLM_USAGE_TOKEN") or env.get("ANTHROPIC_API_KEY")
    if not base or not token:
        raise RuntimeError(
            "resolved provider is `platform` but MOLECULE_LLM_BASE_URL / usage "
            "token is unset — refusing to fall back to a keyless BYOK route"
        )
    if model.startswith("platform/"):
        model = model[len("platform/"):]
    elif model.startswith("platform:"):
        model = model[len("platform:"):]
    if not model:
        model = OPENCLAW_PLATFORM_DEFAULT_MODEL
    return token, base, model, "openai"



def coerce_servable_model(model_str: str) -> str:
    """Return a model id whose provider this adapter can actually route.

    Why this exists
    ---------------
    The shared molecule-runtime ``config.load_config`` defaults the model
    to the generic ``anthropic:claude-opus-4-7`` when neither the
    MODEL/MOLECULE_MODEL/MODEL_PROVIDER env vars NOR a ``model:`` key in
    ``/configs/config.yaml`` are set. On a *fresh* openclaw provision the
    controlplane provisioner writes a stub ``/configs/config.yaml`` with
    no ``model:`` (it only emits one when an operator picked a model),
    and injects no MODEL env — so the runtime hands this adapter
    ``anthropic:claude-opus-4-7``. But ``anthropic`` is NOT in
    ``OPENCLAW_PROVIDERS``: ``resolve_provider_routing`` then falls back
    to checking ``OPENAI_API_KEY``, finds none, and raises
    ``RuntimeError: No API key found for provider 'anthropic'`` — which
    aborts ``setup()`` before the OpenClaw gateway is ever started. The
    workspace registers but is permanently ``not_configured`` and can
    never chat, even if the operator later sets a MiniMax/Kimi/OpenAI
    key (because the *model* is still the unroutable anthropic default).

    A model this adapter cannot structurally serve must never be the
    effective default. When the resolved model's provider prefix isn't
    one we know how to route, coerce to this template's own declared
    default (``OPENCLAW_DEFAULT_MODEL``). An operator who explicitly
    sets MODEL/config.yaml to a real openclaw-registry model
    (openai/groq/openrouter/qianfan/minimax/moonshot) is unaffected —
    that prefix is in the registry, so it passes through untouched.
    Bare model ids (no ``provider:`` prefix) also pass through:
    ``resolve_provider_routing`` already treats them as ``openai:`` which
    IS routable.
    """
    if ":" not in model_str:
        # No provider prefix → resolve_provider_routing treats as openai:* (routable).
        return model_str
    prefix = model_str.split(":", 1)[0]
    if prefix in OPENCLAW_PROVIDERS:
        return model_str
    logger.warning(
        "Configured model %r has provider %r which OpenClaw cannot route "
        "(known: %s); falling back to template default %r. Set MODEL "
        "(or config.yaml model:) to an OpenClaw-supported provider to override.",
        model_str, prefix, ",".join(sorted(OPENCLAW_PROVIDERS)), OPENCLAW_DEFAULT_MODEL,
    )
    return OPENCLAW_DEFAULT_MODEL


# Clean status line shown when an openclaw turn aborts / times out with NO
# assistant text at all (e.g. aborted mid-tool-use before any visible text was
# produced). Deliberately generic + reassuring — an orchestration turn that hits
# the per-turn cap while delegating is still "working", so tell the user that
# rather than dumping the raw envelope or an "error".
_OPENCLAW_ABORT_STATUS_TEXT = (
    "Working on it — delegating to the team; a consolidated update is on the way."
)


def _clean_reply_from_envelope(result: dict, data: dict) -> str:
    """Derive a USER-SAFE reply from a payload-less openclaw ``agent --json``
    envelope (aborted / timed-out run), never the raw run-result object.

    OpenClaw's envelope carries the assistant's own text in
    ``finalAssistantVisibleText`` (and a raw variant in
    ``finalAssistantRawText``) even when the run aborts at the per-turn cap and
    emits no ``result.payloads`` entry. We surface that clean text in preference
    order; only when the engine produced no visible text at all do we fall back
    to a clean status line. We NEVER return ``str(data)`` — that stringifies the
    whole run-result (runId/meta/systemPromptReport/tools/executionTrace/…) into
    the user's chat, which is the demo-critical raw-JSON-dump bug this guards.

    ``finalAssistant*`` normally live under ``result`` (sibling of ``payloads``),
    but we also check top-level ``data`` defensively in case a future envelope
    hoists them.
    """
    for src in (result, data):
        if not isinstance(src, dict):
            continue
        for key in ("finalAssistantVisibleText", "finalAssistantRawText"):
            val = src.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return _OPENCLAW_ABORT_STATUS_TEXT


class OpenClawAdapter(BaseAdapter):

    def __init__(self):
        self._gateway_process = None
        self._mcp_process = None

    @staticmethod
    def name() -> str:
        return "openclaw"

    @staticmethod
    def display_name() -> str:
        return "OpenClaw"

    @staticmethod
    def description() -> str:
        return "OpenClaw agent runtime — Node.js gateway with SOUL/BOOTSTRAP/AGENTS workspace convention"

    @staticmethod
    def get_config_schema() -> dict:
        return {
            "model": {"type": "string", "description": "Model ID (e.g. google/gemini-2.5-flash)"},
            "provider_url": {"type": "string", "description": "LLM provider base URL", "default": "https://openrouter.ai/api/v1"},
            "gateway_port": {"type": "integer", "description": "OpenClaw gateway port", "default": 18789},
        }

    async def setup(self, config: AdapterConfig) -> None:  # pragma: no cover
        """Install OpenClaw, run onboard, copy workspace files, start gateway."""
        # Boot-smoke contract (molecule-core#2275): the publish-image gate
        # invokes us with stub creds + no network so it can exercise lazy
        # imports inside execute(). Real gateway spawn would fail here
        # (no valid api_key, no `openclaw` binary on PATH yet), so skip
        # the heavy setup path entirely. The runtime's smoke_mode short-
        # circuit fires immediately after create_executor() returns.
        if os.environ.get("MOLECULE_SMOKE_MODE") == "1":
            logger.info("MOLECULE_SMOKE_MODE=1 — skipping OpenClaw gateway spawn")
            return

        npm_prefix = os.path.expanduser("~/.local")
        os.environ["PATH"] = f"{npm_prefix}/bin:{os.environ.get('PATH', '')}"

        # 1. Install OpenClaw CLI if not present
        if not shutil.which("openclaw"):
            logger.info("Installing OpenClaw CLI...")
            result = subprocess.run(
                ["npm", "install", "--prefix", npm_prefix, "-g", "openclaw"],
                capture_output=True, text=True, timeout=300,
                env={**os.environ, "npm_config_prefix": npm_prefix}
            )
            if result.returncode != 0:
                raise RuntimeError(f"Failed to install OpenClaw: {result.stderr[:500]}")

            # Install known missing optional deps
            oc_dir = os.path.join(npm_prefix, "lib/node_modules/openclaw")
            if os.path.exists(oc_dir):
                logger.info("Installing OpenClaw optional deps...")
                subprocess.run(
                    ["npm", "install"] + OPENCLAW_MISSING_DEPS,
                    capture_output=True, text=True, timeout=120, cwd=oc_dir
                )
            logger.info("OpenClaw CLI installed")

        # 2. Resolve API key and model
        from molecule_runtime.adapter_base import resolve_provider_routing
        # Never let an unroutable generic default (anthropic:claude-opus-4-7
        # from the shared runtime's load_config) reach a registry that has
        # no `anthropic` entry — that bricks fresh provisions. See
        # coerce_servable_model for the full rationale.
        # A resolved provider of `platform` short-circuits the per-vendor
        # colon registry: route everything through the Molecule proxy. Selected
        # by provider==platform (LLM_PROVIDER/model namespace), NOT a
        # billing-mode env. Checked BEFORE resolve_provider_routing, which would
        # raise on the stripped-key (no-BYOK) platform environment.
        _pm = resolve_platform_routing(config.model, os.environ)
        if _pm is not None:
            api_key, provider_url, model, compatibility = _pm
            logger.info(
                "platform-managed LLM -> proxy %s (model %s, compat openai)",
                provider_url, model,
            )
        else:
            servable_model = coerce_servable_model(config.model)
            api_key, provider_url, model = resolve_provider_routing(
                servable_model, os.environ, registry=OPENCLAW_PROVIDERS, runtime_config=config.runtime_config
            )

            # 2b. CP-proxy-token routing override.
            #
            # Some upstream credentials are issued as Anthropic-compat proxy
            # tokens whose ONLY valid surface is an Anthropic Messages
            # gateway — the provider's native OpenAI-compat endpoint (the
            # registry default) 401s them. We detect such tokens by key
            # shape and, when the resolved route is still the native path,
            # rewrite `provider_url` + flip the onboard compatibility to
            # `anthropic`. We deliberately reuse the EXISTING mechanism:
            # the onboard call below already wires --custom-base-url /
            # --custom-compatibility into ~/.openclaw/openclaw.json, and the
            # step-3c block already writes ~/.openclaw/.../auth-profiles.json
            # keyed off `provider_url`. So routing == mutating these three
            # locals; no new file-writer is introduced. A user supplying a
            # real native JWT (not the proxy prefix) keeps the native path.
            #
            # --- MiniMax token-plan (`sk-cp-*`) ---
            # `sk-cp-*` keys come from molecule-controlplane's claude-proxy.
            # The native Minimax v1 endpoint (api.minimaxi.com/v1) returns
            # `{"base_resp":{"status_code":2049,"status_msg":"invalid api
            # key"}}` -> canvas `FailoverError: HTTP 401`. The proxy IS
            # reachable via Minimax's Anthropic-compat path at
            # api.minimax.io/anthropic (note .io, not .com).
            #
            # --- Kimi For Coding (`sk-kimi-*`) ---
            # `sk-kimi-*` keys are minted at platform.kimi.ai/console/api-keys
            # for Moonshot's "Kimi For Coding" tier. They CANNOT authenticate
            # against the legacy api.moonshot.ai surfaces (the registry
            # default for `moonshot:*`): chat/completions, /v1/models and
            # /anthropic/v1/messages all 401 `invalid_authentication_error`.
            # Their only valid surface is the Anthropic Messages gateway at
            # https://api.kimi.com/coding (messages path /coding/v1/messages).
            # That gateway additionally gates on User-Agent: only coding-agent
            # UAs are accepted (non-coding UAs 403 `access_terminated_error`).
            # OpenClaw's Anthropic SDK shim sends a `claude-cli/<version>` UA
            # on every request, which passes the allowlist, so no extra UA
            # config is needed on our side — same as the MiniMax path. The
            # gateway serves a single model id (`kimi-for-coding`), so we
            # pin the model when we take this route (the canvas writes
            # shapes like `moonshot:kimi-k2` / `kimi-coding/kimi-k2` that
            # the gateway rejects).
            compatibility = "openai"
            # Match the minimax vendor regardless of host (api.minimax.io global
            # vs api.minimaxi.com China) — the default is now the global .io host,
            # so keying on "minimaxi.com" would silently stop firing the sk-cp-
            # anthropic rewrite. "minimax" matches both surfaces.
            if api_key and api_key.startswith("sk-cp-") and "minimax" in provider_url:
                logger.info(
                    "Detected sk-cp- claude-proxy token for native-Minimax route; "
                    "switching to Minimax Anthropic-compat endpoint "
                    "(api.minimax.io/anthropic)"
                )
                provider_url = "https://api.minimax.io/anthropic"
                compatibility = "anthropic"
            elif api_key and api_key.startswith("sk-kimi-") and "moonshot.ai" in provider_url:
                logger.info(
                    "Detected sk-kimi- Kimi-For-Coding token for native-Moonshot "
                    "route; switching to Kimi Anthropic-compat endpoint "
                    "(api.kimi.com/coding, model kimi-for-coding)"
                )
                provider_url = "https://api.kimi.com/coding"
                compatibility = "anthropic"
                model = "kimi-for-coding"

        # 3. Run non-interactive onboard
        if not os.path.exists(os.path.expanduser("~/.openclaw/openclaw.json")):
            logger.info(f"Running OpenClaw onboard (model: {model}, compat: {compatibility})...")
            subprocess.run(
                ["openclaw", "onboard", "--non-interactive",
                 "--auth-choice", "custom-api-key",
                 "--custom-base-url", provider_url,
                 "--custom-model-id", model,
                 "--custom-api-key", api_key,
                 "--custom-compatibility", compatibility,
                 "--secret-input-mode", "plaintext",
                 "--accept-risk", "--skip-health"],
                capture_output=True, text=True, timeout=60,
                env={**os.environ, "NODE_NO_WARNINGS": "1"}
            )
            logger.info("OpenClaw onboard complete")

        # 3b. Fix context window (OpenClaw defaults to 16K, but modern models have much more)
        oc_config_path = os.path.expanduser("~/.openclaw/openclaw.json")
        if os.path.exists(oc_config_path):
            try:
                import json as json_mod
                oc_cfg = json_mod.load(open(oc_config_path))
                provider_name = "custom-" + provider_url.split("//")[1].split("/")[0].replace(".", "-")
                providers = oc_cfg.get("models", {}).get("providers", {})
                if provider_name in providers:
                    for m in providers[provider_name].get("models", []):
                        m["contextWindow"] = 1000000  # 1M tokens for modern models
                        m["maxTokens"] = 16384
                    json_mod.dump(oc_cfg, open(oc_config_path, "w"), indent=2)
                    logger.info(f"Fixed context window for {provider_name}")
            except Exception as e:
                logger.warning(f"Failed to fix context window: {e}")

        # 3c. Always write auth-profiles.json
        # (key may have been set via secrets API after first boot)
        if api_key:
            auth_dir = os.path.expanduser("~/.openclaw/agents/main/agent")
            os.makedirs(auth_dir, exist_ok=True)
            auth_file = os.path.join(auth_dir, "auth-profiles.json")
            import json as json_mod
            provider_name = "custom-" + provider_url.split("//")[1].split("/")[0].replace(".", "-")
            auth_data = {provider_name: {"type": "api-key", "key": api_key}}
            with open(auth_file, "w") as f:
                json_mod.dump(auth_data, f, indent=2)
            logger.info(f"Wrote auth-profiles.json for {provider_name}")

        # 3d. Boot-time auth smoke probe for the configured route.
        #
        # If the upstream rejects the credential at boot we want to fail
        # loudly HERE — surfacing the routing/credential mismatch in the
        # workspace boot log — instead of letting the first canvas
        # message hit a confusing HTTP 401 / FailoverError after the
        # ~7-minute gateway cold-start. Only the auth surface is probed
        # (a single max_tokens=1 request), so it costs ~one token and
        # ~one round trip. Network / non-auth HTTP failures are logged
        # but NOT fatal: the gateway may still be usable and we don't
        # want a transient upstream blip to brick workspace boot.
        #
        # The probe matches the SAME wire shape the gateway will use:
        #  - anthropic-compat routes -> POST {base}/v1/messages with
        #    `anthropic-version` + `x-api-key`.
        #  - openai-compat routes -> POST {base}/chat/completions.
        # ALL routes send a coding-agent `User-Agent` (set just before the
        # request is built). The CP proxy is Cloudflare-fronted and the default
        # `Python-urllib/*` UA trips Cloudflare's browser-integrity rule (403,
        # edge error 1010) — and the Kimi-For-Coding gateway 403s non-coding-
        # agent UAs too. Mirroring the UA the real gateway sends proves
        # end-to-end reachability (auth AND the edge/UA allowlist), not just
        # network, and stops a false 403 from bricking a working concierge.
        try:
            import urllib.request as _urlreq
            import urllib.error as _urlerr
            if compatibility == "anthropic":
                _probe_url = provider_url.rstrip("/") + "/v1/messages"
                _probe_headers = {
                    "anthropic-version": "2023-06-01",
                    "x-api-key": api_key,
                    "content-type": "application/json",
                }
                _probe_body = json.dumps({
                    "model": model.split(":", 1)[-1],
                    "max_tokens": 1,
                    "messages": [{"role": "user", "content": "ping"}],
                }).encode()
            else:
                # OpenAI-compat probe — most registry providers expose
                # /chat/completions at the configured base URL.
                _probe_url = provider_url.rstrip("/") + "/chat/completions"
                _probe_headers = {
                    "Authorization": f"Bearer {api_key}",
                    "content-type": "application/json",
                }
                _probe_body = json.dumps({
                    "model": model.split(":", 1)[-1],
                    "max_tokens": 1,
                    "messages": [{"role": "user", "content": "ping"}],
                }).encode()
            # The CP LLM proxy sits behind Cloudflare. Cloudflare's browser-
            # integrity edge rule 403s a default `Python-urllib/*` User-Agent
            # with error 1010 — a BROWSER-SIGNATURE ban, NOT an LLM-auth failure.
            # Without a real UA this fail-closed probe bricks the concierge even
            # though the gateway's own traffic (Node / claude-cli UA) is served
            # fine. Send the SAME coding-agent UA the real gateway uses so the
            # probe traverses the identical edge path the live traffic will (this
            # also satisfies the Kimi-For-Coding coding-agent-UA allowlist).
            _probe_headers.setdefault("user-agent", "claude-cli/1.0 (molecule-openclaw boot-probe)")
            _req = _urlreq.Request(_probe_url, data=_probe_body, headers=_probe_headers, method="POST")
            try:
                with _urlreq.urlopen(_req, timeout=10) as _resp:
                    _code = _resp.getcode()
                    logger.info(f"Upstream auth smoke probe ok ({_probe_url} -> HTTP {_code})")
            except _urlerr.HTTPError as _he:
                if _he.code in (401, 403):
                    raise RuntimeError(
                        f"Upstream auth smoke probe rejected the configured key at "
                        f"{_probe_url} (HTTP {_he.code}). The provider credential and "
                        f"provider_url={provider_url} are incompatible "
                        f"(a 403 may also be a Cloudflare edge UA ban — error "
                        f"1010 — or, on api.kimi.com, a blocked User-Agent). Fix "
                        f"the secret or the routing override in adapter.py before "
                        f"this workspace can chat."
                    )
                logger.warning(
                    f"Upstream auth smoke probe non-fatal failure at {_probe_url}: "
                    f"HTTP {_he.code}; continuing"
                )
        except RuntimeError:
            raise
        except Exception as _e:
            logger.warning(f"Upstream auth smoke probe could not run ({type(_e).__name__}: {_e}); continuing")

        # 4. Copy workspace files from /configs to OpenClaw's workspace dir
        os.makedirs(OPENCLAW_WORKSPACE, exist_ok=True)
        for fname in os.listdir(config.config_path):
            src = os.path.join(config.config_path, fname)
            if os.path.isfile(src) and fname.endswith(".md"):
                shutil.copy2(src, os.path.join(OPENCLAW_WORKSPACE, fname))
                logger.debug(f"Copied {fname} to OpenClaw workspace")

        # 4b. Register the molecule A2A MCP server with OpenClaw.
        #
        # Why this exists
        # ---------------
        # Without this step OpenClaw only exposes its native tool profile
        # (sessions_list, sessions_send, subagents, ...). When the user
        # asks "can you see your peers" the model reaches for the native
        # `sessions_list` (OpenClaw's own sessions) and cannot see
        # molecule platform peers, because the molecule platform tools
        # (list_peers, get_workspace_info, delegate_task,
        # send_message_to_user, ...) were never wired into OpenClaw's
        # tool loop. This is the OpenClaw analogue of the Hermes #129
        # root cause (generated agent config missing the molecule
        # platform tool registration) — surfaced differently because
        # OpenClaw has a competing native tool the model prefers.
        #
        # Why HTTP transport instead of stdio
        # -----------------------------------
        # molecule_runtime.a2a_mcp_server's stdio main() reads stdin via
        # a blocking `stdin.read(65536)` that does not return until 64KB
        # arrive OR stdin hits EOF. MCP clients (OpenClaw's bundle-mcp,
        # Claude Code, ...) send one small newline-delimited JSON message
        # and keep stdin open, so the stdio server never parses
        # `initialize` and OpenClaw times the MCP handshake out after
        # 30s ("MCP error -32000: Connection closed"). The HTTP+SSE
        # transport (the same one Hermes consumes) has no such bug:
        # `--transport http --port 9100` answers `initialize`
        # immediately. We run it as a supervised sidecar and register it
        # with OpenClaw as a streamable-http MCP server.
        #
        # Diagnosed 2026-05-15 on prod workspace
        # ced1e6b2-b680-4314-816d-2d0cf6b12f71.
        await self._setup_molecule_mcp()

        # 4c. Install DECLARED plugins via the base per-runtime pipeline (P4).
        #
        # Until now this template called the plugin pipeline ZERO times — a dead
        # pipeline. The single hardcoded `_setup_molecule_mcp` above wires only
        # the in-process A2A peer-discovery sidecar (`molecule`, HTTP); it does
        # NOT consume any plugin the org DECLARES in config.yaml. The privileged
        # org-management MCP (`molecule-platform-mcp` → `@molecule-ai/mcp-server`,
        # the create_workspace / org-admin tooling a CONCIERGE needs) is delivered
        # as a declared plugin, so without this call an openclaw concierge boots
        # with peer-discovery but NO management MCP — exactly the cross-runtime
        # gap P4 closes.
        #
        # `install_plugins_via_registry` is the LOOP over declared MCP plugins:
        # for each one it resolves MCPServerAdaptor (runtime-agnostic) →
        # register_mcp_server_hook (our openclaw override below) →
        # mcp_render.render_for_runtime("openclaw", …), which writes the stdio
        # descriptor into ~/.openclaw/openclaw.json `mcp.servers.<name>` (the file
        # `openclaw mcp set` mutates and the gateway re-reads at start in step 5).
        # A privileged-plugin failure raises PrivilegedPluginInstallError so a
        # management-MCP-less concierge fails CLOSED + loudly rather than silent.
        await self._install_declared_mcp_plugins(config)

        # 4d. Materialize the ASSEMBLED platform system prompt into the SOUL.md the
        #     gateway reads. Runs AFTER 4c on purpose: the orchestrator-only
        #     guardrail is gated on the management MCP being wired
        #     (mcp_server_present), which 4c is what installs — so a concierge
        #     reports True here and gets gagged from self-executing, while a worker
        #     reports False and keeps doing real work. This is what turns a fresh
        #     openclaw concierge from generic ("I don't have an identity yet") into
        #     the Org Concierge orchestrator (persona + peers + coordinator + guardrail).
        await self._materialize_persona_into_soul(config)

        # 5. Start the gateway as a background process
        gateway_port = config.runtime_config.get("gateway_port", OPENCLAW_PORT)
        logger.info(f"Starting OpenClaw gateway on port {gateway_port}...")
        env = os.environ.copy()
        env["NODE_NO_WARNINGS"] = "1"
        self._gateway_process = subprocess.Popen(
            ["openclaw", "gateway", "--dev", "--port", str(gateway_port), "--bind", "loopback"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=env,
        )
        # Wait for gateway to become healthy (max 30s)
        for attempt in range(15):
            await asyncio.sleep(2)
            if self._gateway_process.poll() is not None:
                raise RuntimeError("OpenClaw gateway process exited")
            try:
                health = subprocess.run(
                    ["openclaw", "gateway", "health"],
                    capture_output=True, text=True, timeout=10,
                    env=os.environ.copy()
                )
                if health.returncode == 0:
                    logger.info(f"OpenClaw gateway healthy (PID: {self._gateway_process.pid})")
                    break
            except subprocess.TimeoutExpired:
                logger.debug(f"Gateway health check timeout (attempt {attempt+1}/15)")
        else:
            raise RuntimeError("OpenClaw gateway did not become healthy within 30s")

        # 6. Pre-approve operator scopes for the loopback CLI device.
        #
        # Why this exists
        # ---------------
        # OpenClaw 2026.5.6+ enforces per-device scope pairing. The first
        # `openclaw agent` call from a fresh state dir registers the
        # CLI client as a new device with scope `operator.read` only.
        # Any subsequent agent call that needs `operator.write` (which
        # actual prompt execution always does) trips the gateway's
        # scope-upgrade flow and the user sees:
        #
        #   GatewayClientRequestError: scope upgrade pending approval
        #   EMBEDDED FALLBACK: ... pairing required: device is asking
        #   for more scopes than currently approved
        #
        # in the canvas chat instead of a model response, despite the
        # model config being correct and the gateway being healthy.
        #
        # Why we patch paired.json directly instead of using the CLI
        # ---------------------------------------------------------
        # `openclaw devices approve` requires a `requestId`, but
        # `--latest` only PREVIEWS the most recent pending request — it
        # does not approve it (verified live, openclaw 2026.5.6). Every
        # CLI invocation against a not-yet-fully-scoped device opens a
        # new gateway-connect attempt that creates a fresh pending
        # request and obsoletes the previous one, so capturing a
        # requestId from `list` and feeding it to `approve` races
        # against the CLI's own pending-request churn ("unknown
        # requestId" by the time approve runs).
        #
        # The gateway reads paired.json from disk on each connect
        # (verified by clean `pending.json={}` + successful
        # `openclaw agent` after a direct file patch), so writing the
        # approved scope set into paired.json is durable across gateway
        # restarts AND completely avoids the approval-RPC race. The
        # device identity is bound to the CLI's keypair which is
        # already minted by `openclaw onboard` above and is stable
        # across invocations, so we don't need to wait for a pending
        # request to exist — we just patch whatever device row openclaw
        # wrote and grant it the full operator scope set.
        #
        # The full operator scope set is the union of what openclaw
        # 2026.5.6 enumerates at boot (operator.admin, .approvals,
        # .pairing, .read, .write — see dist/devices-cli-*.js
        # `KNOWN_NON_ADMIN_OPERATOR_SCOPES`). operator.admin in
        # approvedScopes auto-implies the rest (see openclaw's `Ae()`
        # in pairing-token-*.js), so a future scope addition by
        # openclaw upstream still works without a template bump.
        #
        # Diagnosed 2026-05-15 from prod workspace
        # ced1e6b2-b680-4314-816d-2d0cf6b12f71. Verified live: with
        # paired.json patched to approvedScopes=[operator.{admin,
        # approvals, pairing, read, write}], `openclaw agent` calls
        # pass the scope-upgrade gate cleanly. (User-visible chat
        # response then depends on the configured upstream model
        # credentials, which are unrelated to this template fix.)
        OPENCLAW_DEVICES_DIR = os.path.expanduser("~/.openclaw/devices")
        PAIRED_JSON = os.path.join(OPENCLAW_DEVICES_DIR, "paired.json")
        PENDING_JSON = os.path.join(OPENCLAW_DEVICES_DIR, "pending.json")
        # The full operator scope set openclaw 2026.5.6 recognises.
        # operator.admin auto-implies read+write per Ae() in
        # pairing-token-*.js; we list the rest explicitly so older
        # openclaw builds that don't honour the admin->all expansion
        # still see each scope.
        FULL_OPERATOR_SCOPES = sorted([
            "operator.admin",
            "operator.approvals",
            "operator.pairing",
            "operator.read",
            "operator.write",
        ])

        if os.path.exists(PAIRED_JSON):
            try:
                import json as _json
                import time as _time
                with open(PAIRED_JSON) as _f:
                    _paired = _json.load(_f)
                _changed = False
                _now_ms = int(_time.time() * 1000)
                for _dev_id, _dev in _paired.items():
                    if _dev.get("approvedScopes") != FULL_OPERATOR_SCOPES:
                        _dev["scopes"] = list(FULL_OPERATOR_SCOPES)
                        _dev["approvedScopes"] = list(FULL_OPERATOR_SCOPES)
                        _tokens = _dev.get("tokens", {})
                        if isinstance(_tokens, dict) and "operator" in _tokens:
                            _tokens["operator"]["scopes"] = list(FULL_OPERATOR_SCOPES)
                            _tokens["operator"]["updatedAtMs"] = _now_ms
                        _changed = True
                if _changed:
                    # Write atomically — the gateway re-reads this file
                    # on every device connect, so a partial write would
                    # race against the next agent call.
                    _tmp = PAIRED_JSON + ".tmp"
                    with open(_tmp, "w") as _f:
                        _json.dump(_paired, _f, indent=2)
                    os.replace(_tmp, PAIRED_JSON)
                    logger.info(
                        f"Granted full operator scopes to {len(_paired)} paired "
                        f"device(s) in {PAIRED_JSON}"
                    )
                # Clear stale pending requests so the gateway's
                # in-memory pending-by-id map doesn't keep returning
                # "scope upgrade pending approval" for requests the
                # device no longer needs after the direct grant.
                if os.path.exists(PENDING_JSON):
                    try:
                        with open(PENDING_JSON) as _f:
                            _pending = _json.load(_f)
                        if _pending:
                            _tmp = PENDING_JSON + ".tmp"
                            with open(_tmp, "w") as _f:
                                _json.dump({}, _f)
                            os.replace(_tmp, PENDING_JSON)
                            logger.info(
                                f"Cleared {len(_pending)} stale pending pairing request(s)"
                            )
                    except (OSError, ValueError) as _e:
                        logger.warning(f"Could not clear pending.json: {_e}")
            except (OSError, ValueError) as _e:
                logger.warning(
                    f"Could not grant operator scopes on {PAIRED_JSON}: {_e}. "
                    "First agent call may hit 'scope upgrade pending approval'."
                )
        else:
            # No paired.json yet — the device hasn't connected to the
            # gateway even once. The gateway will create paired.json on
            # the first device handshake; our execute() fallback below
            # picks up the scope-upgrade error at that point and
            # retries.
            logger.info(
                f"{PAIRED_JSON} not present at setup time; "
                "scope grant will be applied lazily by execute() fallback"
            )

        # 7. Publish the LOADED MCP tool inventory for the core#3082 gate.
        #
        # The platform online/degraded gate (core#3082) wants proof the
        # management MCP's tools were ACTUALLY LOADED (not merely declared). We
        # report the INVENTORY — the tools the registered MCP servers EXPOSE,
        # enumerated once here after the gateway is healthy — NOT the tools the
        # model happened to invoke in a turn. That distinction is the #142/#3082-
        # class bug the fleet just RC'd on codex's per-turn producer: a healthy
        # turn that calls no MCP tool publishes [] and DEGRADES a healthy
        # concierge. An inventory taken from the live server can't false-empty on
        # a tool-less turn. Fail-soft: an enumeration failure publishes nothing
        # (stays None → fail-closed degraded), never a guessed/static list and
        # never an exception into boot.
        await self._publish_loaded_mcp_inventory()

    async def _publish_loaded_mcp_inventory(self):
        """Enumerate the tools the registered MCP servers EXPOSE and publish them
        to the core#3082 ``loaded_mcp_tools`` producer (inventory, not per-turn).

        Reads the registered servers from ~/.openclaw/openclaw.json
        (``mcp.servers``) and, for each STDIO server, runs a one-shot
        ``initialize`` + ``tools/list`` JSON-RPC handshake against the SAME
        command openclaw spawns to get the ground-truth tool list. Publishes the
        union as ``mcp__<server>__<tool>`` ids (matching the prefix the gate
        consumes). HTTP servers (the a2a `molecule` sidecar) are enumerated over
        their URL. Best-effort: any failure leaves the producer at its prior
        value (None until proven), so the gate stays fail-closed rather than
        seeing a guessed list.
        """
        try:
            from molecule_runtime.platform_agent_identity import set_loaded_mcp_tools
        except Exception:  # pragma: no cover — older base image without the producer
            return

        oc_config_path = os.path.expanduser("~/.openclaw/openclaw.json")
        try:
            with open(oc_config_path) as _f:
                servers = (json.load(_f).get("mcp") or {}).get("servers") or {}
        except (OSError, ValueError) as e:
            logger.debug("loaded_mcp_tools: could not read openclaw.json: %s", e)
            return
        if not isinstance(servers, dict) or not servers:
            return

        tool_ids: list[str] = []
        for server_name, spec in servers.items():
            if not isinstance(spec, dict):
                continue
            try:
                names = await self._list_mcp_tools(spec)
            except Exception as e:  # noqa: BLE001 — best-effort enumeration
                logger.debug("loaded_mcp_tools: enumerate %r failed: %s", server_name, e)
                names = []
            for tool in names:
                tool_ids.append(f"mcp__{server_name}__{tool}")

        if not tool_ids:
            # Nothing enumerated — do NOT publish a guessed/empty list; leave the
            # producer at None so the gate stays fail-closed (degraded) until a
            # real inventory is observed (matches the producer contract).
            logger.info(
                "loaded_mcp_tools: no MCP tools enumerated from %d registered "
                "server(s); leaving producer unset (gate stays fail-closed)",
                len(servers),
            )
            return
        try:
            set_loaded_mcp_tools(sorted(set(tool_ids)))
            logger.info(
                "loaded_mcp_tools: published %d-tool inventory from %d server(s)",
                len(set(tool_ids)), len(servers),
            )
        except Exception as e:  # noqa: BLE001
            logger.debug("loaded_mcp_tools: publish failed: %s", e)

    async def _list_mcp_tools(self, spec: dict) -> list[str]:
        """One-shot ``initialize`` + ``tools/list`` against a registered MCP
        server, returning the tool names it exposes. Supports the stdio
        (``{command, args?, env?}``) and http (``{type:http, url}``) shapes
        openclaw stores. Returns [] on any failure (caller logs)."""
        init_req = {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "openclaw-inventory", "version": "1"},
            },
        }
        list_req = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}

        url = spec.get("url")
        if url:
            # HTTP transport (the a2a sidecar). Enumerate over the URL.
            import urllib.request
            tools: list[str] = []
            for req_body in (init_req, list_req):
                req = urllib.request.Request(
                    url,
                    data=json.dumps(req_body).encode(),
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "application/json, text/event-stream",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=8) as resp:
                    body = resp.read().decode(errors="replace")
                if req_body is list_req:
                    tools = _parse_tools_list_body(body)
            return tools

        command = spec.get("command")
        if not command:
            return []
        args = spec.get("args") or []
        child_env = os.environ.copy()
        child_env.setdefault("PATH", f"{os.path.expanduser('~/.local/bin')}:/usr/local/bin:/usr/bin:/bin")
        child_env.update({k: str(v) for k, v in (spec.get("env") or {}).items()})

        proc = await asyncio.create_subprocess_exec(
            command, *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=child_env,
        )
        # Newline-delimited JSON-RPC over stdio: send initialize + tools/list,
        # close stdin, read all stdout. A one-shot enumeration, then the child
        # exits at EOF — we don't keep this process (openclaw owns the real one).
        payload = (json.dumps(init_req) + "\n" + json.dumps(list_req) + "\n").encode()
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(payload), timeout=30)
        except asyncio.TimeoutError:
            proc.kill()
            return []
        return _parse_tools_list_body(stdout.decode(errors="replace"))

    async def _setup_molecule_mcp(self):
        """Start the molecule A2A MCP server (HTTP transport) as a
        supervised sidecar and register it with OpenClaw.

        Fail-fast: if the MCP server does not answer an ``initialize``
        request, raise — a workspace that boots without peer-discovery
        tools is a silent half-failure (the model falls back to native
        ``sessions_list`` and the user sees "only my own session").
        Mirrors the Hermes #129/PR#22 fix shape: register the molecule
        platform tools + assert they are reachable before the runtime
        is considered ready.
        """
        port = OPENCLAW_MCP_HTTP_PORT
        mcp_url = f"http://127.0.0.1:{port}/mcp"

        # Inherit the container env; a2a_mcp_server resolves WORKSPACE_ID
        # / PLATFORM_URL from it (set by the workspace container). PATH is
        # pinned so the absolute interpreter resolves even under the
        # minimal env a supervisor child may get.
        env = os.environ.copy()
        env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")

        logger.info(f"Starting molecule A2A MCP server (HTTP) on port {port}...")
        self._mcp_process = subprocess.Popen(
            [
                "python3", "-m", "molecule_runtime.a2a_mcp_server",
                "--transport", "http", "--port", str(port),
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=env,
        )

        # Fail-fast smoke: the HTTP server must answer `initialize`.
        import urllib.request

        ready = False
        last_err = ""
        for attempt in range(15):
            await asyncio.sleep(1)
            if self._mcp_process.poll() is not None:
                raise RuntimeError(
                    "molecule A2A MCP server exited during startup "
                    f"(rc={self._mcp_process.returncode})"
                )
            try:
                req = urllib.request.Request(
                    mcp_url,
                    data=json.dumps({
                        "jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {},
                            "clientInfo": {"name": "adapter-smoke", "version": "1"},
                        },
                    }).encode(),
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "application/json, text/event-stream",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=4) as resp:
                    body = resp.read().decode(errors="replace")
                if '"serverInfo"' in body and '"molecule"' in body:
                    ready = True
                    logger.info("molecule A2A MCP server healthy (HTTP)")
                    break
                last_err = f"unexpected initialize response: {body[:200]}"
            except Exception as e:  # noqa: BLE001 — smoke loop, retry
                last_err = str(e)
        if not ready:
            raise RuntimeError(
                "molecule A2A MCP server did not become healthy within "
                f"15s — peer-discovery tools would be missing. Last error: {last_err}"
            )

        # Register the molecule MCP server with OpenClaw as a
        # streamable-http server. OpenClaw normalises {"type":"http",
        # "url":...} to {"url":..., "transport":"streamable-http"} and
        # reads this on gateway start.
        result = subprocess.run(
            ["openclaw", "mcp", "set", "molecule",
             json.dumps({"type": "http", "url": mcp_url})],
            capture_output=True, text=True, timeout=30,
            env=os.environ.copy(),
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"`openclaw mcp set molecule` failed (rc={result.returncode}): "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
        logger.info("Registered molecule MCP server with OpenClaw")

    async def _materialize_persona_into_soul(self, config: AdapterConfig) -> None:
        """Assemble the FULL platform system prompt and write it into the SOUL.md
        the openclaw gateway reads, so a fresh concierge boots AS its delivered
        persona rather than the stock OpenClaw SOUL.md.

        The step-4 raw ``/configs/*.md`` copy was insufficient on three counts,
        all fixed here:

          * it NEVER called ``build_system_prompt`` — so the orchestrator-only
            guardrail ("you NEVER do the work yourself"), the resolved platform
            instructions, the peer roster, and the coordinator/children context
            were absent from the model's on-disk identity;
          * it wrote to the wrong dir (fixed by ``_openclaw_workspace_dir``); and
          * the concierge persona is delivered at ``/configs/prompts/concierge.md``
            — a SUBDIR the top-level ``*.md`` copy skips — which
            ``build_system_prompt`` loads via ``config.prompt_files``.

        This is the openclaw arm of the base runtime's persona-materialization
        contract (``_common_setup`` -> ``build_system_prompt`` -> native identity
        file: claude-code=system-prompt.md, openclaw=SOUL.md). We assemble here
        rather than calling ``_common_setup`` wholesale so we do NOT re-run the
        openclaw MCP install pipeline (owned by ``_install_declared_mcp_plugins``).

        Best-effort by design (mirrors the runtime port): any failure logs loudly
        but never bricks boot — the raw copied SOUL.md remains as a fallback.
        """
        try:
            from molecule_runtime.prompt import (
                build_system_prompt,
                get_peer_capabilities,
                get_platform_instructions,
            )
            from molecule_runtime.skill_loader.loader import load_skills
        except Exception as e:  # noqa: BLE001 — older runtime wheel: leave raw SOUL.md
            logger.warning(
                "openclaw persona: runtime prompt builder unavailable (%s); "
                "leaving raw SOUL.md in place", e,
            )
            return

        platform_url = os.environ.get("PLATFORM_URL", "http://host.docker.internal:8080")

        # Skills declared for this workspace (runtime-filtered). Fail-open to none.
        try:
            loaded_skills = load_skills(
                config.config_path, getattr(config, "tools", []) or [],
                current_runtime=self.name(),
            )
        except Exception:  # noqa: BLE001
            loaded_skills = []

        # Peer roster + resolved platform instructions (both fail-open to empty
        # inside the runtime helpers, so a platform blip never bricks boot).
        peers = await get_peer_capabilities(platform_url, config.workspace_id)
        platform_instructions = await get_platform_instructions(platform_url, config.workspace_id)

        # Coordinator/children context (a parent-of-a-team concierge) — same source
        # the base _common_setup uses. Absent/empty for a leaf workspace.
        extra_prompts: list[str] = []
        try:
            from molecule_runtime.coordinator import get_children, build_children_description
            children = await get_children()
            if children:
                extra_prompts.append(build_children_description(children))
        except Exception:  # noqa: BLE001 — coordinator context is additive, never required
            pass

        # Orchestrator-only guardrail gate (platform/concierge ONLY). Ask the
        # runtime whether the privileged management MCP is wired into openclaw's
        # native config — True on a concierge (4c installed it), False on a worker.
        # Default to worker (no guardrail) on any error so a worker is never gagged.
        try:
            from molecule_runtime.platform_agent_identity import mcp_server_present
            is_platform_agent = mcp_server_present()
        except Exception:  # noqa: BLE001
            is_platform_agent = False

        assembled = build_system_prompt(
            config.config_path,
            config.workspace_id,
            loaded_skills,
            peers,
            prompt_files=getattr(config, "prompt_files", None) or None,
            plugin_prompts=extra_prompts or None,
            platform_instructions=platform_instructions,
            platform_guardrail=is_platform_agent,
        )

        # SSOT: publish the single assembled prompt back onto the shared config so
        # any later reader agrees with what the gateway loads.
        config.system_prompt = assembled

        os.makedirs(OPENCLAW_WORKSPACE, exist_ok=True)
        soul_path = os.path.join(OPENCLAW_WORKSPACE, "SOUL.md")
        with open(soul_path, "w") as fh:
            fh.write(assembled if assembled.endswith("\n") else assembled + "\n")
        logger.info(
            "openclaw persona: wrote assembled system prompt to %s "
            "(%d chars, guardrail=%s, %d peer(s), %d skill(s))",
            soul_path, len(assembled), is_platform_agent, len(peers), len(loaded_skills),
        )

    async def _install_declared_mcp_plugins(self, config: AdapterConfig):
        """Drive the base per-runtime plugin pipeline for declared plugins (P4).

        Loads plugins from the per-workspace dir (+ shared fallback) and runs
        ``install_plugins_via_registry``. For an MCP-server plugin (the privileged
        ``molecule-platform-mcp`` a concierge declares) this resolves
        MCPServerAdaptor → ``register_mcp_server_hook`` (overridden below) →
        ``mcp_render.render_for_runtime("openclaw", …)``, rendering the
        ``[mcp.servers.<name>]`` stdio entry into ~/.openclaw/openclaw.json.

        Mirrors the codex adapter's identical call from its own setup(). Without
        it the declared management MCP is NEVER written for openclaw, so the
        concierge boots without create_workspace (the #3159 class of bug — here:
        not wired at all). A PrivilegedPluginInstallError propagates so a
        management-MCP-less concierge fails closed + loudly.
        """
        from molecule_runtime.plugins import load_plugins

        workspace_plugins_dir = os.path.join(config.config_path, "plugins")
        plugins = load_plugins(
            workspace_plugins_dir=workspace_plugins_dir,
            shared_plugins_dir=os.environ.get("PLUGINS_DIR", "/plugins"),
        )
        if plugins.plugin_names:
            logger.info("openclaw: installing declared plugins: %s",
                        ", ".join(plugins.plugin_names))
        await self.install_plugins_via_registry(config, plugins)

    def register_mcp_server_hook(self, config, name, spec):
        """OpenClaw MCP-wiring PORT override: render the openclaw-native config.

        Writes the stdio descriptor into ~/.openclaw/openclaw.json
        ``mcp.servers.<name>`` — the same on-disk shape ``openclaw mcp set``
        produces — NOT ``.claude/settings.json`` (the #3159 mis-attribution where
        an openclaw concierge's management MCP is rendered into, and judged
        against, a file its runtime never reads).

        We render DIRECTLY here rather than delegating to the base hook's
        ``mcp_render.render_for_runtime("openclaw", …)``: that dispatch is a
        deliberate fail-loud ``NotImplementedError`` stub on runtime versions that
        predate the openclaw renderer (format-unverified, phase P4), which would
        crash the management-MCP install on any not-yet-graduated runtime even
        though we DO know the format. Owning the renderer in the template keeps the
        install path working across pinned runtime versions and keeps it byte-shape
        identical to the runtime's own ``render_openclaw_config``.

        OpenClaw spawns each stdio MCP server as a child and passes the
        descriptor's ``env`` block to it. The management MCP
        (``@molecule-ai/mcp-server``) reads MOLECULE_CP_URL + MOLECULE_ADMIN_TOKEN
        to reach the controlplane; if they aren't in the descriptor's env,
        create_workspace 401s/no-ops even though the server is declared. So we
        resolve those values at install time and merge them as LITERALS into the
        spec's ``env`` before writing — the exact pattern the codex adapter uses
        for its config.toml env sub-table. Descriptor-declared keys (e.g.
        MOLECULE_MCP_MODE) win and are never overwritten.
        """
        spec = dict(spec)
        descriptor_env = dict(spec.get("env") or {})
        for key in (
            "MOLECULE_CP_URL",
            "MOLECULE_ADMIN_TOKEN",
            "MOLECULE_API_URL",
            "MOLECULE_API_KEY",
            "MOLECULE_ORG_API_KEY",
            "MOLECULE_ORG_SLUG",
            "MOLECULE_AUDIT_ACTOR",
            "PLATFORM_URL",
            "WORKSPACE_ID",
            "MOLECULE_ORG_ID",
        ):
            if key in descriptor_env:
                continue
            val = os.environ.get(key)
            if val:
                descriptor_env[key] = val
        if descriptor_env:
            spec["env"] = descriptor_env
        config_path = _openclaw_config_path()
        _render_openclaw_mcp(config_path, name, spec)
        logger.info(
            "register_mcp_server_hook: wired MCP %r into %s (runtime=openclaw)",
            name, config_path,
        )

    def management_mcp_present(self, config) -> bool:
        """True when the privileged management MCP is wired into openclaw's native
        config (~/.openclaw/openclaw.json ``mcp.servers``).

        Runtime-agnostic answer to the RCA#2970 online gate's "is the management
        MCP wired?" probe, judged against the file openclaw actually reads rather
        than a Claude settings.json it never reads (#3159). Overrides the base —
        which dispatches to the runtime's present-reader — for the same reason
        ``register_mcp_server_hook`` does: on runtimes predating the openclaw
        renderer the base present-reader reports False even after we wrote the
        file, disagreeing with what the renderer produced.

        Fail-closed by construction (a missing/malformed config yields False), so
        a genuinely MCP-less concierge stays degraded at the gate."""
        return _openclaw_mcp_present(_openclaw_config_path(), MANAGEMENT_MCP_NAME)

    def mcp_settings_path(self, config) -> str:
        """Absolute native MCP-config file openclaw reads its servers from
        (~/.openclaw/openclaw.json), owned by the adapter (ADR-004 socket
        ``mcp_settings_path``).

        Overrides the base — which dispatches to the runtime's
        ``mcp_settings_path_for`` — for the SAME runtime-version reason
        ``register_mcp_server_hook`` / ``management_mcp_present`` do: the adapter
        owns the openclaw native surface directly so the path agrees with what the
        renderer writes / the present-probe + reader read, on every pinned runtime.
        ``config`` (config_path) is unused: openclaw resolves ``$HOME`` at call
        time, mirroring ``_openclaw_config_path``."""
        return _openclaw_config_path()

    async def enumerate_loaded_mcp_tools(self, config) -> "list[str] | None":
        """Enumerate the LOADED MCP tool ids openclaw actually has, or None
        (ADR-004 socket ``enumerate_loaded_mcp_tools``).

        Overrides the base default (which reads openclaw's servers via the
        engine's ``read_mcp_servers_for`` switch) to read openclaw's OWN native
        config directly (``_read_openclaw_mcp_servers``) and hand the resolved
        ``{name: spec}`` map to the shared boot-safe probe engine
        (``enumerate_from_specs_async``). This co-locates the reader with the
        renderer/present-probe in the adapter (the render→read→present triangle is
        internally consistent) and keeps enumerate working on runtime versions
        whose engine reader could be a ``{}`` stub — the same runtime-version
        rationale as the render override.

        TRI-STATE + BOOT-SAFE + NEVER-RAISES are provided by
        ``enumerate_from_specs_async``:
          * ``None``  — nothing observable (no servers declared, or every probe
            failed/stalled/unreadable). Producer left unset → grace window.
          * ``[]``    — a server connected and advertised zero tools.
          * ``[ids]`` — deduped/sorted ``mcp__<server>__<tool>`` ids."""
        from molecule_runtime.loaded_mcp_tools_probe import (
            enumerate_from_specs_async,
        )

        servers = _read_openclaw_mcp_servers(_openclaw_config_path())
        return await enumerate_from_specs_async(servers)

    def materialize_persona(self, config: AdapterConfig):
        """Materialize the workspace's CANONICAL persona into openclaw's native
        identity file ``<configs>/SOUL.md`` (ADR-004 socket ``materialize_persona``).

        The persona-file PORT (distinct from ``_materialize_persona_into_soul``,
        which assembles the FULL platform system prompt into the gateway WORKSPACE
        dir at setup): this writes the raw canonical persona into the top-level
        ``/configs/SOUL.md`` and clears the BOOTSTRAP.md / AGENTS.md placeholders,
        byte-identical to the runtime engine's ``materialize_openclaw_persona``.
        Overrides the base — which dispatches through
        ``persona_render.materialize_persona_for`` — to own the openclaw
        convention directly (ADR-004 relocates per-runtime shape into the adapter).

        Best-effort, fail-SOFT (a persona is not a privileged capability): reads
        the canonical persona runtime-agnostically from ``config.prompt_files``
        (via the engine's generic ``read_canonical_persona`` helper, which carries
        no runtime name and stays in the engine), returns ``None`` (no-op) when no
        persona is delivered — never clobbering openclaw's baked default with an
        empty identity — and returns the SOUL.md path on success."""
        from molecule_runtime import persona_render

        persona = persona_render.read_canonical_persona(
            config.config_path, config.prompt_files
        )
        if not (persona or "").strip():
            logger.info(
                "materialize_persona: no canonical persona delivered for openclaw "
                "— leaving the runtime's native default (baked SOUL.md) untouched"
            )
            return None
        target = _materialize_openclaw_persona(config.config_path, persona)
        logger.info(
            "materialize_persona: wrote openclaw persona (%d chars) to %s",
            len(persona), target,
        )
        return target

    async def create_executor(self, config: AdapterConfig) -> AgentExecutor:
        # Pass the workspace identity so the shared base derives a STABLE,
        # workspace-keyed session id (not the per-request context_id the a2a-sdk
        # mints fresh each turn) — tenant-agent BUG 3.
        return OpenClawA2AExecutor(
            workspace_id=getattr(config, "workspace_id", "") or "",
            heartbeat=config.heartbeat,
        )


def _oc_text(content) -> str:
    """Join the text of openclaw content blocks (``[{type:'text', text}]``)."""
    if isinstance(content, list):
        return "".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    if isinstance(content, str):
        return content
    return ""


def _openclaw_steps(data: dict) -> list:
    """Extract ordered turn steps (SSOT AgentTrace.steps shape — {kind:
    'thinking'|'tool_call', text/name/input/result}) from openclaw's turn.

    VERIFIED against a live workspace (openclaw 2026.x, MiniMax): the
    ``agent --json`` envelope does NOT inline the transcript. Instead
    ``result.meta.agentMeta.sessionFile`` points at a per-session JSONL that
    this adapter — running in the SAME container — reads directly. Each line is
    a typed event; the ``message`` events carry the ordered turn:

      * ``role='assistant'`` → ``content[]`` blocks of ``{type:'text'|'thinking'
        |'reasoning'}`` (reasoning / final answer) and ``{type:'toolCall', id,
        name, arguments}``.
      * ``role='toolResult'`` → ``toolCallId`` + ``content[].text`` /
        ``details.aggregated`` (the tool RESULT — the field claude-code's SDK
        cannot fill, so openclaw traces are strictly richer).

    We splice each tool result back onto its call by ``toolCallId`` and emit
    ``thinking`` / ``tool_call`` steps in transcript order. FAIL-OPEN and
    defensive: a missing field, unreadable file, or any shape change yields
    ``[]`` — the shared tracer still emits the turn output + prompt pieces.
    """
    try:
        meta = (data.get("result") or {}).get("meta") or {}
        sess = ((meta.get("agentMeta") or {}).get("sessionFile")) or ""
        if not sess or not os.path.exists(sess):
            return []
        rows = []
        with open(sess) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
        # Pass 1: index tool results by call id.
        results = {}
        for r in rows:
            m = r.get("message") if isinstance(r, dict) else None
            if isinstance(m, dict) and m.get("role") == "toolResult":
                cid = m.get("toolCallId")
                txt = _oc_text(m.get("content")) or (m.get("details") or {}).get("aggregated") or ""
                if cid:
                    results[cid] = txt
        # Pass 2: ordered thinking + tool_call steps from assistant turns.
        steps = []
        for r in rows:
            m = r.get("message") if isinstance(r, dict) else None
            if not isinstance(m, dict) or m.get("role") != "assistant":
                continue
            for blk in (m.get("content") or []):
                if not isinstance(blk, dict):
                    continue
                bt = blk.get("type")
                if bt == "toolCall":
                    args = blk.get("arguments")
                    steps.append({
                        "kind": "tool_call",
                        "name": str(blk.get("name") or "tool"),
                        "input": json.dumps(args) if isinstance(args, (dict, list))
                        else str(args or blk.get("partialArgs") or ""),
                        "result": results.get(blk.get("id")) or None,
                    })
                elif bt in ("thinking", "reasoning"):
                    t = blk.get("thinking") or blk.get("text") or blk.get("reasoning") or ""
                    if t.strip():
                        steps.append({"kind": "thinking", "text": t})
                elif bt == "text":
                    t = blk.get("text") or ""
                    if t.strip():
                        steps.append({"kind": "thinking", "text": t})
        return steps
    except Exception:
        return []


async def _communicate_touching_lease(proc, *, timeout):
    """Drain ``proc``'s stdout+stderr to EOF while renewing the turn lease on
    every chunk, then wait for exit. Drop-in for
    ``asyncio.wait_for(proc.communicate(), timeout)`` returning
    ``(stdout_bytes, stderr_bytes)``.

    This is turn-lease SOURCE D (subprocess-output liveness): the OpenClaw
    executor spawns ``openclaw agent`` as a subprocess and blocks on it, so it
    never runs the native runtime's ``on_tool_start``/``on_tool_end`` lease
    touches (source A) and openclaw does not write
    ``MOLECULE_TOOL_ACTIVITY_FILE`` (source C) — leaving a long OpenClaw turn
    with NO lease renewal. Any bytes the child emits on stdout/stderr are a
    proxy for "still working", so each chunk calls ``turn_lease.touch_current()``
    and keeps the lease fresh (up to its TTL / absolute cap). The plain
    ``communicate()`` buffered ALL output until exit, so no incremental signal
    ever reached the lease.

    ``turn_lease.touch_current()`` is a no-op when the mailbox kernel is off (no
    lease installed), so the default (non-kernel) flow is behaviourally
    identical to ``communicate()`` — it just reads incrementally.

    ``timeout`` is a wall-clock cap over the whole read+wait. On timeout the
    child is killed (closing the leak the old ``wait_for(communicate())`` left —
    it cancelled the read but left the process running) and
    ``asyncio.TimeoutError`` is re-raised so the caller's existing timeout
    branch is unchanged.
    """
    out = bytearray()
    err = bytearray()

    async def _pump(stream, sink):
        if stream is None:
            return
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                break
            sink.extend(chunk)
            # Source D: incremental subprocess output renews the lease. No-op
            # when no lease is installed (mailbox kernel off) or the runtime
            # predates the mailbox kernel.
            _lease_touch()

    async def _drain_and_wait():
        await asyncio.gather(_pump(proc.stdout, out), _pump(proc.stderr, err))
        await proc.wait()

    try:
        await asyncio.wait_for(_drain_and_wait(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await proc.wait()
        except Exception:  # noqa: BLE001 — reaping the killed child must not mask the timeout
            pass
        raise
    return bytes(out), bytes(err)


class OpenClawA2AExecutor(SubprocessA2AExecutor):
    """Shells A2A turns out to the ``openclaw agent`` CLI subprocess.

    tenant-agent BUG 3: the SESSION contract is INHERITED from
    ``SubprocessA2AExecutor`` — the openclaw ``--session-id`` is derived from the
    STABLE workspace identity (not the per-request ``context_id`` the a2a-sdk mints
    fresh each turn), so openclaw's native session RESUMES across turns. Continuity
    is that native session; conversation history is NOT force-injected into the
    task text — the base passes ONLY the current user message to ``run_agent``, and
    older history is retrieved only if the agent chooses to call a platform-workspace
    MCP tool. This class provides ONLY:

      * ``_decorate_message`` — the openclaw ``MEDIA:`` image lines, and
      * ``run_agent``         — the ``openclaw agent`` shell-out (+ turn-lease
                                liveness + the scope-upgrade self-heal retry).

    It MUST NOT override ``execute()``; the contract lives in the base and is
    guarded by the shared contract test.
    """

    runtime_label = "OpenClaw"

    def _decorate_message(self, user_message, attached):
        # Surface image attachments to OpenClaw's media parser via MEDIA: lines.
        # Adorns the CURRENT user message only; the base does not prepend any
        # prior-turn transcript, so what reaches the CLI is this turn's message
        # plus its media refs (continuity is openclaw's own resumed session).
        image_media_lines = [
            f"MEDIA: {f['path']}"
            for f in attached
            if (f.get("mime_type") or "").startswith("image/") and f.get("path")
        ]
        if image_media_lines:
            user_message = (
                user_message.rstrip() + "\n\n" + "\n".join(image_media_lines)
            ).strip()
        return user_message

    async def run_agent(self, task_text, session_id, context):
        # Arm the turn lease (MUST-FIX 1) at the start of this OpenClaw turn.
        # OpenClaw runs its whole turn inside a single blocking `openclaw agent`
        # subprocess and never enters the native runtime's astream/idle-cap +
        # on_tool_start/end lease-touch path, so without this the lease is never
        # reset per turn. No-op when the mailbox kernel is off / the runtime
        # predates it. The lease is then renewed on subprocess output (source D)
        # by _communicate_touching_lease below.
        _lease_reset()
        # Ordered turn steps (SSOT AgentTrace.steps) the shared TracingExecutor
        # reads; reset per turn so a JSON parse failure cannot leak prior steps.
        self._last_steps = []

        # task_text is the CURRENT user message only (+ any MEDIA: lines from
        # _decorate_message) — the base does NOT inject conversation history;
        # continuity comes from openclaw resuming the native session keyed on
        # session_id, the STABLE workspace-keyed id (base: derive_session_id).
        #
        # Call OpenClaw agent via CLI, retrying once if we hit the
        # scope-upgrade-pending gate. The priming step in setup() should
        # cover steady-state, but openclaw can re-request scopes after
        # gateway restarts, session expiry, or a CLI version bump. The
        # retry-with-approve here makes the turn self-healing so a single
        # canvas message can recover from a stale pairing without a restart.
        reply = None
        # OpenClaw's gateway rejects a --session-id containing ':' with
        # "Invalid session ID" (GatewayClientRequestError), which fails EVERY
        # A2A turn on an openclaw concierge. The shared derive_session_id emits
        # the stable "workspace:<uuid>" form (fine for claude-code/codex/hermes
        # native sessions). Map ':' -> '-' for the OpenClaw CLI only: the mapping
        # is DETERMINISTIC so the native session still RESUMES across turns, and
        # "workspace-<uuid>" is accepted by the gateway (verified on a live
        # baked concierge: colon form -> Invalid session ID; dash form -> ok).
        oc_session_id = session_id.replace(":", "-")
        try:
            for attempt in range(2):
                proc = await asyncio.create_subprocess_exec(
                    "openclaw", "agent",
                    "--session-id", oc_session_id,
                    "--message", task_text,
                    "--json", "--timeout", "120",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env={**os.environ, "PATH": f"{os.path.expanduser('~/.local/bin')}:{os.environ.get('PATH', '')}"}
                )
                # Renew the turn lease on every chunk of subprocess output
                # (source D) so a genuinely-working long OpenClaw turn is not
                # mistaken for a stall. Behaviourally identical to
                # communicate() when the mailbox kernel is off.
                stdout, stderr = await _communicate_touching_lease(proc, timeout=130)
                output = stdout.decode().strip()
                stderr_text = stderr.decode() if stderr else ""

                if proc.returncode == 0 and output:
                    try:
                        data = json.loads(output)
                        result = data.get("result", {}) if isinstance(data, dict) else {}
                        payloads = result.get("payloads", []) if isinstance(result, dict) else []
                        if payloads:
                            reply = payloads[0].get("text", "")
                        else:
                            # Payload-less envelope — the run aborted / hit the
                            # per-turn timeout (status:timeout, aborted:true,
                            # stopReason:toolUse) BEFORE emitting an assistant
                            # payload. NEVER stringify the whole run-result object
                            # into the user's chat: str(data) dumps
                            # runId/meta/systemPromptReport/tools/executionTrace…
                            # verbatim (the demo-critical raw-JSON-dump bug).
                            # Surface only the clean assistant text the engine
                            # already computed, or a clean status line.
                            reply = _clean_reply_from_envelope(result, data)
                        # SSOT AgentTrace.steps: openclaw runs the tool loop
                        # itself, so its --json envelope can carry the full
                        # transcript: thinking + tool_use + tool RESULT. The
                        # shared TracingExecutor reads `_last_steps`; extraction
                        # is fail-open across openclaw output shapes.
                        try:
                            self._last_steps = _openclaw_steps(data)
                        except Exception:
                            self._last_steps = []
                    except json.JSONDecodeError:
                        reply = output
                    break

                # Detect openclaw's pairing/scope-upgrade gate. The
                # error surfaces in either stdout or stderr depending
                # on whether --json saw enough to emit a JSON envelope
                # first. Match on either the structured code or the
                # human-readable banner so we catch both shapes.
                combined = (output + "\n" + stderr_text).lower()
                is_pairing_error = (
                    "scope upgrade pending approval" in combined
                    or "pairing required" in combined
                    or "asking for more scopes than" in combined
                )
                if is_pairing_error and attempt == 0:
                    logger.info("openclaw agent hit pairing/scope-upgrade gate; granting scopes via paired.json and retrying")
                    # Direct on-disk grant. `openclaw devices approve
                    # --latest` only previews — see the long comment
                    # in setup() above for why we patch the file
                    # instead of using the CLI. Best-effort: if the
                    # patch itself fails (file missing, JSON unreadable,
                    # disk full) we fall through and surface the
                    # original error to the user, rather than failing
                    # silently.
                    try:
                        _devices_dir = os.path.expanduser("~/.openclaw/devices")
                        _paired_path = os.path.join(_devices_dir, "paired.json")
                        _pending_path = os.path.join(_devices_dir, "pending.json")
                        _scopes = sorted([
                            "operator.admin",
                            "operator.approvals",
                            "operator.pairing",
                            "operator.read",
                            "operator.write",
                        ])
                        import time as _time
                        _now_ms = int(_time.time() * 1000)
                        if os.path.exists(_paired_path):
                            with open(_paired_path) as _f:
                                _paired = json.load(_f)
                            for _dev in _paired.values():
                                _dev["scopes"] = list(_scopes)
                                _dev["approvedScopes"] = list(_scopes)
                                _tokens = _dev.get("tokens", {})
                                if isinstance(_tokens, dict) and "operator" in _tokens:
                                    _tokens["operator"]["scopes"] = list(_scopes)
                                    _tokens["operator"]["updatedAtMs"] = _now_ms
                            _tmp = _paired_path + ".tmp"
                            with open(_tmp, "w") as _f:
                                json.dump(_paired, _f, indent=2)
                            os.replace(_tmp, _paired_path)
                            if os.path.exists(_pending_path):
                                _tmp = _pending_path + ".tmp"
                                with open(_tmp, "w") as _f:
                                    json.dump({}, _f)
                                os.replace(_tmp, _pending_path)
                            logger.info("Granted full operator scopes on paired.json; retrying agent call")
                    except (OSError, ValueError) as _e:
                        logger.warning(f"Could not grant scopes in execute() fallback: {_e}")
                    continue

                reply = (
                    f"OpenClaw error: {stderr_text[:300]}"
                    if stderr_text
                    else f"OpenClaw returned code {proc.returncode}"
                )
                break

        except asyncio.TimeoutError:
            reply = "OpenClaw timed out after 120s"
        # NOTE: any other exception propagates to the base execute(), which
        # logs it and surfaces "OpenClaw error: <e>" as the reply.
        return reply


Adapter = OpenClawAdapter
# no-op: retrigger CI
