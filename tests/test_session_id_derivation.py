"""Session-id derivation + NO-history-injection tests (tenant-agent BUG 3).

OpenClawA2AExecutor is a THIN subclass of the shared ``SubprocessA2AExecutor``
base — it implements ONLY ``run_agent`` (the ``openclaw agent`` shell-out) and
``_decorate_message`` (the ``MEDIA:`` image lines). It does NOT override
``execute()``; the session CONTRACT lives in the base and is INHERITED:

  * STABLE SESSION ID — the openclaw ``--session-id`` is derived from the STABLE
    workspace identity (``workspace:<WORKSPACE_ID>``), NOT the per-request
    ``context_id`` the a2a-sdk mints fresh each turn (keying on ``context_id``
    opened a NEW native session every message → the agent re-greeted). So
    openclaw's own SessionManager RESUMES the same session across turns.
    ``context_id`` / ``task_id`` / ``"default"`` remain fallbacks ONLY when no
    WORKSPACE_ID is available.

  * NO HISTORY INJECTION — continuity is that native, resumed session, NOT a
    force-injected transcript. The base passes ONLY the CURRENT user message to
    ``run_agent`` and deliberately does NOT prepend ``metadata.history`` to the
    CLI ``--message``. (The previous approach injected history; it double-fed
    context, grew the prompt unboundedly, and fought openclaw's own memory.)
    Older/other history is retrieved ONLY if the agent CHOOSES to call a
    platform-workspace MCP tool (e.g. ``get_conversation_history``) that reads
    the persisted activity store — it is never shoved into every task text.

These tests capture the ``openclaw agent`` CLI args and assert on ``--session-id``
and ``--message`` — no openclaw CLI subprocess, no network.
"""
from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

if "adapter" not in sys.modules:
    pytest.skip(
        "adapter.py not importable (molecule_runtime missing) — "
        "matches canonical validator soft-skip",
        allow_module_level=True,
    )
adapter = sys.modules["adapter"]

# The shared base owns execute() (message extraction, image enrichment, session
# id, heartbeat). Patch its collaborators there, not on the thin adapter module.
import molecule_runtime.subprocess_executor as _base


class _FakeStream:
    """Minimal async byte stream for _communicate_touching_lease's _pump()."""

    def __init__(self, data: bytes):
        self._data = data
        self._sent = False

    async def read(self, _n: int = -1) -> bytes:
        if self._sent:
            return b""
        self._sent = True
        return self._data


class _FakeProc:
    """Stands in for the openclaw subprocess. Supports the drain path the base
    uses (proc.stdout/stderr .read() + proc.wait())."""

    def __init__(self, stdout=b'{"result": {"payloads": [{"text": "ok"}]}}', stderr=b""):
        self.returncode = 0
        self.stdout = _FakeStream(stdout)
        self.stderr = _FakeStream(stderr)

    async def wait(self):
        return self.returncode


def _build_context(*, task_id, context_id, history=None, text="hi"):
    ctx = MagicMock()
    ctx.task_id = task_id
    ctx.context_id = context_id
    msg = MagicMock()
    text_part = MagicMock()
    text_part.text = text
    text_part.kind = "text"
    msg.parts = [text_part]
    msg.metadata = None
    ctx.message = msg
    # A prior-turn transcript IS present in the request metadata. The contract is
    # that the base does NOT read it into the task text — so these tests supply it
    # precisely to prove it never reaches the CLI --message.
    ctx.request = SimpleNamespace(metadata={"history": history or []})
    ctx.metadata = {"history": history or []}
    return ctx


def _patch_cli(monkeypatch):
    """Patch the CLI spawn + heartbeat; return the captured-args list."""
    captured = []

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured.append(args)
        return _FakeProc()

    monkeypatch.setattr(adapter.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    # execute() lives in the base; neutralize its heartbeat push there.
    monkeypatch.setattr(_base, "set_current_task", AsyncMock(return_value=None))
    # Neutralize the turn-lease side effects in the unit context.
    monkeypatch.setattr(adapter, "_lease_reset", lambda: None, raising=False)
    return captured


async def _run(ex, ctx):
    queue = MagicMock()
    queue.enqueue_event = AsyncMock()
    await ex.execute(ctx, queue)


def _session_arg(args):
    return args[args.index("--session-id") + 1]


def _message_arg(args):
    return args[args.index("--message") + 1]


@pytest.mark.asyncio
async def test_session_id_is_workspace_keyed_and_stable(monkeypatch):
    """The session id is workspace-keyed and STABLE across fresh context_ids."""
    captured = _patch_cli(monkeypatch)
    ex = adapter.OpenClawA2AExecutor(workspace_id="ws-stable-1", heartbeat=None)

    await _run(ex, _build_context(task_id="task-a", context_id="ctx-aaaa"))
    await _run(ex, _build_context(task_id="task-b", context_id="ctx-bbbb"))

    # The base derives the STABLE "workspace:<id>" session key, but run_agent
    # sanitizes ':' -> '-' for the openclaw CLI (the gateway rejects a colon in
    # --session-id with "Invalid session ID"). The mapping is DETERMINISTIC, so
    # the native session still RESUMES across turns — the CLI arg is the dash form.
    assert _session_arg(captured[0]) == "workspace-ws-stable-1"
    assert _session_arg(captured[1]) == "workspace-ws-stable-1"  # stable, not ctx-bbbb


@pytest.mark.asyncio
async def test_session_id_colon_is_sanitized_to_dash_for_gateway(monkeypatch):
    """The workspace-keyed session id reaches the openclaw CLI with ':' -> '-'.

    OpenClaw's gateway rejects a --session-id containing ':' with "Invalid session
    ID" (GatewayClientRequestError), which failed EVERY A2A turn on an openclaw
    concierge (verified live). run_agent maps ':' -> '-' for the CLI only; the map
    is deterministic so the native session still resumes. This pins that the colon
    NEVER reaches the CLI arg.
    """
    captured = _patch_cli(monkeypatch)
    ex = adapter.OpenClawA2AExecutor(workspace_id="ws-colon-1", heartbeat=None)
    await _run(ex, _build_context(task_id="t", context_id="c"))
    session_arg = _session_arg(captured[0])
    assert ":" not in session_arg  # the gateway would 400 on a colon
    assert session_arg == "workspace-ws-colon-1"


@pytest.mark.asyncio
async def test_session_id_falls_back_to_context_id_without_workspace(monkeypatch):
    captured = _patch_cli(monkeypatch)
    ex = adapter.OpenClawA2AExecutor(workspace_id="", heartbeat=None)
    ex._workspace_id = ""  # force the no-identity fallback path deterministically
    await _run(ex, _build_context(task_id="task-changes", context_id="chat-stable"))
    assert _session_arg(captured[0]) == "chat-stable"


@pytest.mark.asyncio
async def test_session_id_falls_back_to_task_id_then_default(monkeypatch):
    captured = _patch_cli(monkeypatch)
    ex = adapter.OpenClawA2AExecutor(workspace_id="", heartbeat=None)
    ex._workspace_id = ""
    await _run(ex, _build_context(task_id="task-only", context_id=None))
    assert _session_arg(captured[0]) == "task-only"

    captured.clear()
    ex2 = adapter.OpenClawA2AExecutor(workspace_id="", heartbeat=None)
    ex2._workspace_id = ""
    await _run(ex2, _build_context(task_id=None, context_id=None))
    assert _session_arg(captured[0]) == "default"


@pytest.mark.asyncio
async def test_history_is_NOT_injected_into_message(monkeypatch):
    """metadata.history is NOT prepended to the CLI --message (no-inject contract).

    Continuity is openclaw's native session resumed via the stable workspace-keyed
    ``--session-id`` (asserted here too) — the transcript is never force-fed into
    the task text. A prior-turn history IS supplied on the context; the assertion
    is that NONE of it leaks into ``--message``, which carries ONLY this turn's
    user message.
    """
    captured = _patch_cli(monkeypatch)
    ex = adapter.OpenClawA2AExecutor(workspace_id="ws-1", heartbeat=None)
    history = [
        {"role": "user", "parts": [{"text": "my name is Ada"}]},
        {"role": "agent", "parts": [{"text": "Hello Ada"}]},
    ]
    await _run(
        ex,
        _build_context(task_id="t", context_id="c", history=history, text="what's my name?"),
    )

    message = _message_arg(captured[0])
    # ONLY the current turn's message reaches the CLI …
    assert message == "what's my name?"
    # … and NONE of the prior-turn transcript is injected.
    assert "my name is Ada" not in message
    assert "Hello Ada" not in message
    assert "Conversation so far:" not in message  # old build_task_text framing is gone
    # Continuity instead rides on the STABLE workspace-keyed session id — passed to
    # the openclaw CLI in its gateway-accepted, ':'->'-' sanitized dash form.
    assert _session_arg(captured[0]) == "workspace-ws-1"


@pytest.mark.asyncio
async def test_history_not_injected_even_across_turns(monkeypatch):
    """Each turn's --message is that turn's message only; no accumulation."""
    captured = _patch_cli(monkeypatch)
    ex = adapter.OpenClawA2AExecutor(workspace_id="ws-1", heartbeat=None)

    await _run(ex, _build_context(task_id="t1", context_id="c1", text="first"))
    await _run(ex, _build_context(task_id="t2", context_id="c2", text="second"))

    assert _message_arg(captured[0]) == "first"
    assert _message_arg(captured[1]) == "second"  # not "first\n...second"
    # Same resumed native session both turns (continuity source) — the CLI receives
    # the ':'->'-' sanitized dash form the gateway accepts.
    assert _session_arg(captured[0]) == _session_arg(captured[1]) == "workspace-ws-1"


def test_decorate_message_adds_media_lines_for_images():
    """Unit: _decorate_message surfaces image attachments as MEDIA: lines."""
    ex = adapter.OpenClawA2AExecutor(workspace_id="ws-1", heartbeat=None)
    out = ex._decorate_message(
        "look at this",
        [
            {"name": "shape-probe.png", "mime_type": "image/png", "path": "/w/shape-probe.png"},
            {"name": "doc.txt", "mime_type": "text/plain", "path": "/w/doc.txt"},
        ],
    )
    assert "look at this" in out
    assert "MEDIA: /w/shape-probe.png" in out
    assert "doc.txt" not in out  # only images get a MEDIA line


@pytest.mark.asyncio
async def test_image_attachment_flows_into_cli_message(monkeypatch):
    """End-to-end: an image attachment reaches the CLI --message as a MEDIA line.

    The file-URI resolver (executor_helpers.extract_attached_files) needs platform
    env to resolve URIs; that is orthogonal to this adapter, so we patch it in the
    base to return an already-resolved image dict and assert the MEDIA line reaches
    the CLI through the base execute() -> _decorate_message flow. No conversation
    history is prepended — only this turn's message plus its MEDIA line.
    """

    async def _noop_vision(text, files):
        return text

    monkeypatch.setattr(
        _base,
        "extract_attached_files",
        lambda _message: [
            {"name": "shape-probe.png", "mime_type": "image/png", "path": "/w/shape-probe.png"}
        ],
    )
    monkeypatch.setattr(_base, "append_image_descriptions", _noop_vision)
    captured = _patch_cli(monkeypatch)

    ex = adapter.OpenClawA2AExecutor(workspace_id="ws-1", heartbeat=None)
    await _run(ex, _build_context(task_id="task-1", context_id="chat-1", text="look"))

    message = _message_arg(captured[0])
    assert "shape-probe.png" in message
    assert "MEDIA: /w/shape-probe.png" in message
    assert message.startswith("look")  # current message first, MEDIA line appended
