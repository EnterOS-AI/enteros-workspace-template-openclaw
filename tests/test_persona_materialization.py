"""Persona-materialization tests (BUG B — openclaw concierge boots generic).

Two defects made a fresh openclaw concierge run the STOCK OpenClaw SOUL.md and
self-identify generically instead of as the Org Concierge:

  * TARGET DIR — the adapter materialized the persona into
    ``~/.openclaw/workspace-dev/main``, a dir the gateway never reads. The gateway
    reads ``~/.openclaw/workspace`` (openclaw's ``resolveDefaultAgentWorkspaceDir``,
    partitioned by ``OPENCLAW_PROFILE`` only). ``_openclaw_workspace_dir`` fixes it.

  * ASSEMBLED PROMPT — the adapter only raw-copied ``/configs/*.md`` and never
    called ``build_system_prompt``, so the orchestrator-only guardrail + peers +
    coordinator context were absent, and the concierge persona
    (``/configs/prompts/concierge.md`` — a SUBDIR the top-level copy skips) never
    reached SOUL.md. ``_materialize_persona_into_soul`` assembles + writes it.
"""
from __future__ import annotations

import os
import sys

import pytest

if "adapter" not in sys.modules:
    pytest.skip(
        "adapter.py not importable (molecule_runtime missing) — "
        "matches canonical validator soft-skip",
        allow_module_level=True,
    )
adapter = sys.modules["adapter"]


class _Cfg:
    """Minimal AdapterConfig stand-in for _materialize_persona_into_soul."""

    def __init__(self, config_path, workspace_id, prompt_files):
        self.config_path = config_path
        self.workspace_id = workspace_id
        self.prompt_files = prompt_files
        self.tools = []
        self.system_prompt = None


def test_openclaw_workspace_dir_is_default_workspace(monkeypatch):
    """Default (no OPENCLAW_PROFILE) → ~/.openclaw/workspace, NOT workspace-dev/main."""
    monkeypatch.delenv("OPENCLAW_PROFILE", raising=False)
    d = adapter._openclaw_workspace_dir()
    assert d.replace("\\", "/").endswith("/.openclaw/workspace")
    assert "workspace-dev" not in d
    assert not d.rstrip("/").endswith("/main")


def test_openclaw_workspace_dir_honors_profile(monkeypatch):
    """A non-default OPENCLAW_PROFILE partitions to ~/.openclaw/workspace-<profile>."""
    monkeypatch.setenv("OPENCLAW_PROFILE", "dev")
    assert adapter._openclaw_workspace_dir().replace("\\", "/").endswith("/.openclaw/workspace-dev")
    monkeypatch.setenv("OPENCLAW_PROFILE", "default")  # 'default' is the un-partitioned case
    assert adapter._openclaw_workspace_dir().replace("\\", "/").endswith("/.openclaw/workspace")


@pytest.mark.asyncio
async def test_persona_assembled_into_soul_with_guardrail(tmp_path, monkeypatch):
    """A concierge's persona + orchestrator guardrail land in the workspace SOUL.md.

    Uses the REAL build_system_prompt; only its network/identity collaborators are
    stubbed. Asserts the delivered persona text AND the orchestrator-only guardrail
    are present in the file the gateway reads.
    """
    # Delivered concierge identity, exactly as CP writes it to /configs.
    configs = tmp_path / "configs"
    (configs / "prompts").mkdir(parents=True)
    (configs / "prompts" / "concierge.md").write_text(
        "# You are ACME Agent — the Org Concierge\n\n"
        "You are the organization's platform agent and orchestrator.\n"
    )

    # Redirect the SOUL.md target to a temp workspace dir.
    workspace = tmp_path / "workspace"
    monkeypatch.setattr(adapter, "OPENCLAW_WORKSPACE", str(workspace))

    # Stub the runtime collaborators: no network, and force the concierge (platform)
    # gate True so the orchestrator-only guardrail is injected.
    import molecule_runtime.prompt as _prompt
    import molecule_runtime.platform_agent_identity as _identity

    async def _no_peers(*_a, **_k):
        return []

    async def _no_instr(*_a, **_k):
        return ""

    monkeypatch.setattr(_prompt, "get_peer_capabilities", _no_peers)
    monkeypatch.setattr(_prompt, "get_platform_instructions", _no_instr)
    monkeypatch.setattr(_identity, "mcp_server_present", lambda: True)

    cfg = _Cfg(str(configs), "ws-acme-1", ["prompts/concierge.md"])
    ad = adapter.OpenClawAdapter()
    await ad._materialize_persona_into_soul(cfg)

    soul = workspace / "SOUL.md"
    assert soul.exists(), "SOUL.md must be written to the gateway's workspace dir"
    body = soul.read_text()
    # The delivered persona reached SOUL.md …
    assert "ACME Agent — the Org Concierge" in body
    # … layered under the assembled platform frame …
    assert "Molecule AI platform" in body
    # … WITH the orchestrator-only guardrail (concierge gate was True).
    assert "you NEVER do the work yourself" in body.lower() or "NEVER do the work yourself" in body
    # SSOT: the assembled prompt is published back on the config.
    assert cfg.system_prompt == body.rstrip("\n") or cfg.system_prompt == body


@pytest.mark.asyncio
async def test_worker_soul_has_no_orchestrator_guardrail(tmp_path, monkeypatch):
    """A non-platform (worker) openclaw workspace is NOT gagged with the guardrail."""
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "system-prompt.md").write_text("# Worker\nDo the assigned build work.\n")
    workspace = tmp_path / "workspace"
    monkeypatch.setattr(adapter, "OPENCLAW_WORKSPACE", str(workspace))

    import molecule_runtime.prompt as _prompt
    import molecule_runtime.platform_agent_identity as _identity

    async def _no_peers(*_a, **_k):
        return []

    async def _no_instr(*_a, **_k):
        return ""

    monkeypatch.setattr(_prompt, "get_peer_capabilities", _no_peers)
    monkeypatch.setattr(_prompt, "get_platform_instructions", _no_instr)
    monkeypatch.setattr(_identity, "mcp_server_present", lambda: False)

    cfg = _Cfg(str(configs), "ws-worker-1", [])
    ad = adapter.OpenClawAdapter()
    await ad._materialize_persona_into_soul(cfg)

    body = (workspace / "SOUL.md").read_text()
    assert "NEVER do the work yourself" not in body  # workers keep doing real work
