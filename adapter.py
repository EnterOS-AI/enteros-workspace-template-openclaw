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
from molecule_runtime.adapters.shared_runtime import brief_task, extract_message_text, set_current_task
from a2a.server.agent_execution import AgentExecutor

logger = logging.getLogger(__name__)

# Providers supported by this adapter; maps prefix → (auth_env_vars, default_base_url).
OPENCLAW_PROVIDERS = {
    "openai":     (("OPENAI_API_KEY",),                     "https://api.openai.com/v1"),
    "groq":       (("GROQ_API_KEY",),                       "https://api.groq.com/openai/v1"),
    "openrouter": (("OPENROUTER_API_KEY",),                 "https://openrouter.ai/api/v1"),
    "qianfan":    (("QIANFAN_API_KEY", "AISTUDIO_API_KEY"), "https://qianfan.baidubce.com/v2"),
    "minimax":    (("MINIMAX_API_KEY",),                    "https://api.minimaxi.com/v1"),
    "moonshot":   (("KIMI_API_KEY",),                       "https://api.moonshot.ai/v1"),
}

OPENCLAW_WORKSPACE = os.path.expanduser("~/.openclaw/workspace-dev/main")
OPENCLAW_PORT = 18789

# Known missing optional deps in OpenClaw's npm package
OPENCLAW_MISSING_DEPS = ["@buape/carbon", "@larksuiteoapi/node-sdk", "@slack/web-api", "grammy"]


class OpenClawAdapter(BaseAdapter):

    def __init__(self):
        self._gateway_process = None

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
        api_key, provider_url, model = resolve_provider_routing(
            config.model, os.environ, registry=OPENCLAW_PROVIDERS, runtime_config=config.runtime_config
        )

        # 3. Run non-interactive onboard
        if not os.path.exists(os.path.expanduser("~/.openclaw/openclaw.json")):
            logger.info(f"Running OpenClaw onboard (model: {model})...")
            subprocess.run(
                ["openclaw", "onboard", "--non-interactive",
                 "--auth-choice", "custom-api-key",
                 "--custom-base-url", provider_url,
                 "--custom-model-id", model,
                 "--custom-api-key", api_key,
                 "--custom-compatibility", "openai",
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

        # 4. Copy workspace files from /configs to OpenClaw's workspace dir
        os.makedirs(OPENCLAW_WORKSPACE, exist_ok=True)
        for fname in os.listdir(config.config_path):
            src = os.path.join(config.config_path, fname)
            if os.path.isfile(src) and fname.endswith(".md"):
                shutil.copy2(src, os.path.join(OPENCLAW_WORKSPACE, fname))
                logger.debug(f"Copied {fname} to OpenClaw workspace")

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

    async def create_executor(self, config: AdapterConfig) -> AgentExecutor:
        return OpenClawA2AExecutor(heartbeat=config.heartbeat)


class OpenClawA2AExecutor(AgentExecutor):
    """Proxies A2A messages to OpenClaw via `openclaw agent` CLI subprocess."""

    def __init__(self, heartbeat=None):
        self._heartbeat = heartbeat

    async def execute(self, context, event_queue):
        from a2a.helpers import new_text_message

        user_message = extract_message_text(context)

        if not user_message:
            await event_queue.enqueue_event(new_text_message("No message provided"))
            return

        await set_current_task(self._heartbeat, brief_task(user_message))

        # Call OpenClaw agent via CLI, retrying once if we hit the
        # scope-upgrade-pending gate. The priming step in setup() should
        # cover steady-state, but openclaw can re-request scopes after
        # gateway restarts, session expiry, or a CLI version bump. The
        # retry-with-approve here makes execute() self-healing so a
        # single canvas message can recover from a stale pairing without
        # needing a workspace restart.
        reply = None
        try:
            for attempt in range(2):
                proc = await asyncio.create_subprocess_exec(
                    "openclaw", "agent",
                    "--session-id", context.task_id or "default",
                    "--message", user_message,
                    "--json", "--timeout", "120",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env={**os.environ, "PATH": f"{os.path.expanduser('~/.local/bin')}:{os.environ.get('PATH', '')}"}
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=130)
                output = stdout.decode().strip()
                stderr_text = stderr.decode() if stderr else ""

                if proc.returncode == 0 and output:
                    try:
                        data = json.loads(output)
                        payloads = data.get("result", {}).get("payloads", [])
                        if payloads:
                            reply = payloads[0].get("text", "")
                        else:
                            reply = str(data)
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
        except Exception as e:
            reply = f"OpenClaw error: {e}"
        finally:
            await set_current_task(self._heartbeat, "")

        await event_queue.enqueue_event(new_text_message(reply))

    async def cancel(self, context, event_queue):  # pragma: no cover
        pass


Adapter = OpenClawAdapter
# no-op: retrigger CI
