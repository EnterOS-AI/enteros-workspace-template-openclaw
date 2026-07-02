"""Regression test for the OpenClaw turn-lease liveness gap (turn-lease source D).

Before the fix, OpenClawA2AExecutor.execute() ran the whole turn inside a
single blocking ``openclaw agent`` subprocess and read its output with
``asyncio.wait_for(proc.communicate(), timeout=130)``. ``communicate()``
buffers ALL output until the child exits, and the OpenClaw executor never runs
the native runtime's ``on_tool_start``/``on_tool_end`` lease touches (source A)
nor writes ``MOLECULE_TOOL_ACTIVITY_FILE`` (source C) — so a long OpenClaw turn
produced NO lease renewal at all. If OpenClaw were ever routed through the
idle-cap path, a genuinely-working long turn with no native events would be
falsely killed at the idle-cap.

``_communicate_touching_lease`` closes that gap by draining stdout/stderr
incrementally and calling ``turn_lease.touch_current()`` on every chunk
(source D: subprocess-output liveness). These tests assert:
  1. the lease is touched on each chunk of subprocess output,
  2. the full output (incl. the final JSON envelope) is still captured, and
  3. a hung child is killed and ``asyncio.TimeoutError`` re-raised on timeout.

Soft-skips when molecule_runtime is not installed (adapter not importable) —
the same posture as the canonical validator and the other openclaw tests.
"""
from __future__ import annotations

import asyncio
import sys

import pytest

if "adapter" not in sys.modules:
    pytest.skip(
        "adapter.py not importable (molecule_runtime missing) — "
        "matches canonical validator soft-skip",
        allow_module_level=True,
    )
adapter = sys.modules["adapter"]

# A child that emits 3 timed stdout chunks then a final JSON line + one stderr
# line, modelling openclaw agent --json streaming progress before its result.
_EMIT_CHILD = (
    "import sys, time\n"
    "for i in range(3):\n"
    "    sys.stdout.write('chunk%d\\n' % i); sys.stdout.flush(); time.sleep(0.2)\n"
    "sys.stdout.write('{\"result\": \"done\"}\\n'); sys.stdout.flush()\n"
    "sys.stderr.write('progress\\n'); sys.stderr.flush()\n"
)
_HANG_CHILD = "import time\nwhile True: time.sleep(1)\n"


@pytest.mark.asyncio
async def test_communicate_touches_lease_per_chunk_and_captures_output(monkeypatch):
    """Each chunk of child output renews the lease; full output is captured."""
    from molecule_runtime import turn_lease

    touches = {"n": 0}
    monkeypatch.setattr(turn_lease, "touch_current", lambda: touches.__setitem__("n", touches["n"] + 1))

    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", _EMIT_CHILD,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await adapter._communicate_touching_lease(proc, timeout=15)

    assert b'{"result": "done"}' in stdout, "final JSON envelope must be captured"
    assert b"chunk0" in stdout and b"chunk2" in stdout, "all streamed chunks captured"
    assert b"progress" in stderr, "stderr must be captured"
    assert proc.returncode == 0
    # >=4: one touch per stdout chunk (3 chunks + final line) plus the stderr line.
    assert touches["n"] >= 4, f"lease must be touched per output chunk, got {touches['n']}"


@pytest.mark.asyncio
async def test_communicate_kills_and_reraises_on_timeout(monkeypatch):
    """A hung child is killed (no leak) and TimeoutError re-raised on timeout."""
    from molecule_runtime import turn_lease

    monkeypatch.setattr(turn_lease, "touch_current", lambda: None)

    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", _HANG_CHILD,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    with pytest.raises(asyncio.TimeoutError):
        await adapter._communicate_touching_lease(proc, timeout=1.0)

    # The old wait_for(communicate()) cancelled the read but left the child
    # running; the fix kills + reaps it, so returncode is set.
    await asyncio.wait_for(proc.wait(), timeout=5)
    assert proc.returncode is not None, "hung child must be killed + reaped on timeout"


@pytest.mark.asyncio
async def test_touch_current_is_noop_safe_when_kernel_off():
    """With no lease installed (mailbox kernel off) the helper still works —
    turn_lease.touch_current() is a no-op, so behaviour equals communicate()."""
    from molecule_runtime import turn_lease

    turn_lease.install(None)  # ensure no lease is installed (kernel-off shape)
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import sys; sys.stdout.write('ok')",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await adapter._communicate_touching_lease(proc, timeout=10)
    assert stdout == b"ok"
    assert proc.returncode == 0
