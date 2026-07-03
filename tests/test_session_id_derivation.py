"""Session-id derivation + history-injection regression tests (tenant-agent BUG 3).

OpenClawA2AExecutor now INHERITS the session+history contract from the shared
``SubprocessA2AExecutor`` base:

  * the openclaw ``--session-id`` is derived from the STABLE workspace identity
    (``workspace:<WORKSPACE_ID>``), NOT the per-request ``context_id`` the a2a-sdk
    mints fresh each turn (using ``context_id`` opened a new native session every
    message); ``context_id`` / ``task_id`` / ``"default"`` remain fallbacks only
    when no WORKSPACE_ID is available; and
  * conversation history from ``metadata.history`` is injected into the CLI
    ``--message`` (``build_task_text``) so a 2nd turn keeps context.

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


def _build_context(*, task_id, context_id, history=None):
    ctx = MagicMock()
    ctx.task_id = task_id
    ctx.context_id = context_id
    msg = MagicMock()
    text_part = MagicMock()
    text_part.text = "hi"
    text_part.kind = "text"
    msg.parts = [text_part]
    msg.metadata = None
    ctx.message = msg
    # extract_history reads context.request.metadata / context.metadata.
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
    monkeypatch.setattr(adapter, "set_current_task", AsyncMock(return_value=None))
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

    assert _session_arg(captured[0]) == "workspace:ws-stable-1"
    assert _session_arg(captured[1]) == "workspace:ws-stable-1"  # stable, not ctx-bbbb


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
async def test_history_is_injected_into_message(monkeypatch):
    """metadata.history is prepended to the CLI --message (a 2nd turn keeps context)."""
    captured = _patch_cli(monkeypatch)
    ex = adapter.OpenClawA2AExecutor(workspace_id="ws-1", heartbeat=None)
    history = [
        {"role": "user", "parts": [{"text": "my name is Ada"}]},
        {"role": "agent", "parts": [{"text": "Hello Ada"}]},
    ]
    await _run(ex, _build_context(task_id="t", context_id="c", history=history))

    message = _message_arg(captured[0])
    assert "my name is Ada" in message
    assert "Hello Ada" in message
    assert "Conversation so far:" in message  # build_task_text framing


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
    the CLI through the base execute() -> _decorate_message -> build_task_text flow.
    """
    import molecule_runtime.subprocess_executor as _base

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
    await _run(ex, _build_context(task_id="task-1", context_id="chat-1"))

    message = _message_arg(captured[0])
    assert "shape-probe.png" in message
    assert "MEDIA: /w/shape-probe.png" in message
