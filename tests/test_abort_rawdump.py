"""Regression test for the demo-critical raw-JSON-dump-on-abort bug.

When an openclaw ``agent --json`` turn aborts / hits the per-turn timeout it
returns an envelope with an EMPTY ``result.payloads`` but a populated
``finalAssistantVisibleText``. The old code did ``reply = str(data)`` for the
payload-less branch, which dumped the ENTIRE run-result object
(runId/meta/systemPromptReport/tools/executionTrace/…) into the user's chat.

These tests pin ``_clean_reply_from_envelope`` — the pure helper the fix routes
the payload-less branch through — to prove it surfaces ONLY clean text and never
the raw envelope.

Import note: like every other test in this repo, ``adapter`` is loaded by
``conftest.py`` (repo-root on sys.path, adapter.py loaded as a top-level module).
We intentionally do NOT stub ``molecule_runtime`` here — doing so would replace
the real ``SubprocessA2AExecutor`` base in ``sys.modules`` for the whole session
and break sibling tests that construct ``OpenClawA2AExecutor``.
"""

import adapter


# The exact aborted-run envelope shape from the demo incident: status:timeout,
# aborted:true, stopReason:toolUse, empty payloads, visible text populated.
ABORTED_ENVELOPE = {
    "result": {
        "runId": "run_abc123",
        "meta": {"model": "minimax/MiniMax-M2.7"},
        "systemPromptReport": {"chars": 48213},
        "tools": ["delegate_task", "list_peers"],
        "executionTrace": [{"tool": "delegate_task"}],
        "status": "timeout",
        "aborted": True,
        "stopReason": "toolUse",
        "payloads": [],
        "finalAssistantVisibleText": "All three reports are online. Delegating now.",
        "finalAssistantRawText": "All three reports are online. Delegating now. [raw]",
    }
}


def test_returns_visible_text_not_raw_object():
    reply = adapter._clean_reply_from_envelope(
        ABORTED_ENVELOPE["result"], ABORTED_ENVELOPE
    )
    assert reply == "All three reports are online. Delegating now."
    # The raw envelope internals must NEVER leak into the reply.
    for leaked in ("runId", "systemPromptReport", "executionTrace", "stopReason", "meta"):
        assert leaked not in reply


def test_falls_back_to_raw_text_when_no_visible_text():
    env = {"result": {"payloads": [], "finalAssistantRawText": "partial answer"}}
    reply = adapter._clean_reply_from_envelope(env["result"], env)
    assert reply == "partial answer"


def test_clean_status_line_when_no_text_at_all():
    env = {"result": {"payloads": [], "status": "timeout", "aborted": True}}
    reply = adapter._clean_reply_from_envelope(env["result"], env)
    assert reply == adapter._OPENCLAW_ABORT_STATUS_TEXT
    assert "{" not in reply and "runId" not in reply


def test_never_stringifies_dict():
    # Even a totally empty/garbage envelope must not yield a dict repr.
    reply = adapter._clean_reply_from_envelope({}, {"runId": "x", "meta": {}})
    assert reply == adapter._OPENCLAW_ABORT_STATUS_TEXT
    assert "runId" not in reply
