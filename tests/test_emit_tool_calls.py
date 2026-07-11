"""ADR-004 tool-call display — openclaw adapter emit_tool_call wiring.

The workspace canvas renders a tool-call chip ONLY from an ``agent_log`` activity
row POSTed to ``{PLATFORM_URL}/workspaces/{WORKSPACE_ID}/activity`` (core#2636).
Before ADR-004 only the claude-code template emitted these rows; openclaw's
canvas showed a bare spinner with no visible tool activity. openclaw runs its own
tool loop inside the shelled-out ``openclaw agent`` subprocess and the per-tool
transcript is reconstructed POST-turn from the session JSONL (``_openclaw_steps``).
This adapter replays each parsed ``tool_call`` step into the SDK-owned engine
primitive ``molecule_runtime.tool_trace.emit_tool_call`` at its only correct
tool site — right after ``_last_steps`` is populated in ``run_agent``.

These tests pin the ADAPTER half of the fix (task item 2 — "each ADAPTER calls
it"): the ``_emit_tool_steps`` replay helper. The ENGINE half (that
``emit_tool_call`` POSTs the exact six-key agent_log shape) is pinned by the
runtime's own unit test; the SDK conformance suite (``test_conformance.py``,
inherited from ``molecule_plugin.adapter_conformance``) is the cross-runtime gate.

Import note: like every other test in this repo, ``adapter`` is loaded by
``conftest.py`` (repo-root on sys.path, adapter.py loaded as a top-level module).
Soft-skips when molecule_runtime is not installed (adapter not importable) — the
same posture as the canonical validator / the other openclaw tests.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

import pytest

if "adapter" not in sys.modules:
    pytest.skip(
        "adapter.py not importable (molecule_runtime missing) — "
        "matches canonical validator soft-skip",
        allow_module_level=True,
    )
adapter = sys.modules["adapter"]


# ------------------------------------------------------------------ #
# _emit_tool_steps: replay parsed steps into emit_tool_call
# ------------------------------------------------------------------ #
def _install_recorder(monkeypatch):
    """Replace the adapter's ``_emit_tool_call`` indirection with a recorder
    that captures each (name) it is called with. Returns the capture list.

    Monkeypatching the adapter's own module-level ``_emit_tool_call`` (not
    ``molecule_runtime.tool_trace.emit_tool_call``) keeps the test runnable on
    runtime wheels that predate ADR-004 too — the same posture the turn-lease
    tests use for ``_lease_touch``."""
    calls: list[str] = []

    async def _fake_emit(name, summary=None, status="ok"):
        calls.append(name)

    monkeypatch.setattr(adapter, "_emit_tool_call", _fake_emit)
    return calls


@pytest.mark.asyncio
async def test_emits_one_row_per_tool_call_step(monkeypatch):
    """Every ``kind == 'tool_call'`` step yields exactly one emit_tool_call;
    ``thinking`` steps are skipped (they are not tool calls)."""
    calls = _install_recorder(monkeypatch)
    steps = [
        {"kind": "thinking", "text": "Let me look at the file."},
        {"kind": "tool_call", "name": "Read", "input": '{"path": "/a"}', "result": "ok"},
        {"kind": "thinking", "text": "Now run it."},
        {"kind": "tool_call", "name": "Bash", "input": '{"cmd": "ls"}', "result": "a\nb"},
    ]

    await adapter._emit_tool_steps(steps)

    # One row per tool_call, in transcript order; thinking steps produce none.
    assert calls == ["Read", "Bash"]


@pytest.mark.asyncio
async def test_passes_only_tool_name_engine_supplies_summary(monkeypatch):
    """The adapter passes ONLY the tool name — the engine's ``summarize_tool``
    supplies the generic ``🛠 name(…)`` summary, so the adapter never fabricates
    one. (Recorded call carries the name; summary is the engine's concern.)"""
    calls = _install_recorder(monkeypatch)
    await adapter._emit_tool_steps(
        [{"kind": "tool_call", "name": "delegate_task", "input": "{}", "result": None}]
    )
    assert calls == ["delegate_task"]


@pytest.mark.asyncio
async def test_skips_steps_with_no_name(monkeypatch):
    """A malformed tool_call step with a falsy/absent name emits nothing (never
    an ``emit_tool_call(name='')`` — the engine would no-op that anyway, but the
    adapter must not even try)."""
    calls = _install_recorder(monkeypatch)
    await adapter._emit_tool_steps(
        [
            {"kind": "tool_call", "name": "", "input": "{}"},
            {"kind": "tool_call", "input": "{}"},  # no name key at all
            {"kind": "tool_call", "name": "GoodTool", "input": "{}"},
        ]
    )
    assert calls == ["GoodTool"]


@pytest.mark.asyncio
async def test_empty_and_none_steps_noop(monkeypatch):
    """Empty list / None steps (the fail-open ``_openclaw_steps`` output) emit
    nothing and never raise."""
    calls = _install_recorder(monkeypatch)
    await adapter._emit_tool_steps([])
    await adapter._emit_tool_steps(None)
    assert calls == []


@pytest.mark.asyncio
async def test_best_effort_swallows_emit_errors(monkeypatch):
    """A raising ``emit_tool_call`` for one step MUST NOT abort the replay or
    propagate — telemetry can never break a turn. The subsequent good step
    still emits."""
    calls: list[str] = []

    async def _boom_then_ok(name, summary=None, status="ok"):
        if name == "Boom":
            raise RuntimeError("platform wedged")
        calls.append(name)

    monkeypatch.setattr(adapter, "_emit_tool_call", _boom_then_ok)
    # Must not raise even though the first tool's emit blows up.
    await adapter._emit_tool_steps(
        [
            {"kind": "tool_call", "name": "Boom", "input": "{}"},
            {"kind": "tool_call", "name": "Survivor", "input": "{}"},
        ]
    )
    assert calls == ["Survivor"]


@pytest.mark.asyncio
async def test_noop_when_primitive_absent_old_runtime(monkeypatch):
    """On a runtime wheel predating ADR-004 (``_emit_tool_call is None``) the
    replay is a silent no-op — the adapter must run unchanged on old runtimes."""
    monkeypatch.setattr(adapter, "_emit_tool_call", None)
    # No recorder to assert against; the contract is simply "does not raise".
    await adapter._emit_tool_steps(
        [{"kind": "tool_call", "name": "Read", "input": "{}"}]
    )


# ------------------------------------------------------------------ #
# End-to-end: a real openclaw --json envelope + session JSONL flows
# through _openclaw_steps -> _emit_tool_steps and produces the emits.
# This proves the parse + emit seam an actual turn exercises.
# ------------------------------------------------------------------ #
@pytest.mark.asyncio
async def test_openclaw_steps_then_emit_end_to_end(monkeypatch):
    """A simulated session JSONL (assistant toolCall blocks + toolResult rows)
    parses via ``_openclaw_steps`` and every tool_call replays into
    ``emit_tool_call`` — the exact seam ``run_agent`` walks after a --json turn."""
    calls = _install_recorder(monkeypatch)

    # Session JSONL: one assistant turn with a thinking block + two toolCalls,
    # plus their toolResult rows — the shape _openclaw_steps parses.
    session_rows = [
        {"message": {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "I will read then list."},
            {"type": "toolCall", "id": "tc1", "name": "Read",
             "arguments": {"path": "/etc/hosts"}},
            {"type": "toolCall", "id": "tc2", "name": "Bash",
             "arguments": {"cmd": "ls -la"}},
        ]}},
        {"message": {"role": "toolResult", "toolCallId": "tc1",
                     "content": [{"type": "text", "text": "127.0.0.1 localhost"}]}},
        {"message": {"role": "toolResult", "toolCallId": "tc2",
                     "content": [{"type": "text", "text": "a\nb"}]}},
    ]

    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
        for row in session_rows:
            f.write(json.dumps(row) + "\n")
        session_path = f.name

    try:
        data = {"result": {"meta": {"agentMeta": {"sessionFile": session_path}}}}
        steps = adapter._openclaw_steps(data)
        # Sanity: parse found both tool calls (proves the fixture matches the
        # real JSONL shape the adapter reads).
        tool_names = [s["name"] for s in steps if s.get("kind") == "tool_call"]
        assert tool_names == ["Read", "Bash"]

        await adapter._emit_tool_steps(steps)
        # The two tool calls each produced exactly one activity emit; the
        # thinking block produced none.
        assert calls == ["Read", "Bash"]
    finally:
        os.unlink(session_path)
