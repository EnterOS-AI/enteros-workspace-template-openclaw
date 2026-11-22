"""Tests for Kubernetes foreign-owned ~/.openclaw state-dir isolation (internal#1333)."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

if "adapter" not in sys.modules:
    pytest.skip(
        "adapter.py not importable (molecule_runtime missing) — matches canonical validator soft-skip",
        allow_module_level=True,
    )
adapter = sys.modules["adapter"]

REPO = Path(__file__).resolve().parents[1]
if str(REPO / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO / "scripts"))
import assert_concierge_mcp_loaded as asserter  # noqa: E402


def _cfg(tmp_path: Path):
    from molecule_runtime.adapter_base import AdapterConfig

    return AdapterConfig(
        model="google/gemini-4-hydrogen",
        config_path=str(tmp_path / "configs"),
        workspace_id="ws-k8s-test",
    )


def test_user_owned_openclaw_root_keeps_default_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("OPENCLAW_STATE_DIR", raising=False)
    oc = tmp_path / ".openclaw"
    oc.mkdir()
    real_stat = os.stat

    def fake_stat(p, *args, **kwargs):
        if Path(p) == oc:
            return SimpleNamespace(st_uid=1000, st_mode=0o40700)
        return real_stat(p, *args, **kwargs)

    monkeypatch.setattr(os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(os, "stat", fake_stat)

    assert Path(adapter._openclaw_state_dir()) == oc
    assert Path(adapter._openclaw_config_path()) == oc / "openclaw.json"
    assert asserter.default_openclaw_json() == oc / "openclaw.json"


def test_foreign_owned_k8s_subpath_redirects_to_state_subdir(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("OPENCLAW_STATE_DIR", raising=False)
    oc = tmp_path / ".openclaw"
    oc.mkdir()

    real_stat = os.stat

    def fake_stat(p, *args, **kwargs):
        if Path(p) == oc:
            return SimpleNamespace(st_uid=0, st_mode=0o42777)
        return real_stat(p, *args, **kwargs)

    monkeypatch.setattr(os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(os, "stat", fake_stat)

    expected_state = oc / "state"
    assert Path(adapter._openclaw_state_dir()) == expected_state
    assert expected_state.is_dir()
    assert Path(adapter._openclaw_config_path()) == expected_state / "openclaw.json"

    env = adapter._openclaw_cli_env({"EXTRA_VAR": "1"})
    assert env["OPENCLAW_STATE_DIR"] == str(expected_state)
    assert env["EXTRA_VAR"] == "1"

    ad = adapter.OpenClawAdapter()
    cfg = _cfg(tmp_path)
    ad.register_mcp_server_hook(cfg, adapter.MANAGEMENT_MCP_NAME, {"command": "npx"})
    assert (expected_state / "openclaw.json").is_file()
    assert not (oc / "openclaw.json").exists()
    assert ad.management_mcp_present(cfg) is True
    assert asserter.default_openclaw_json() == expected_state / "openclaw.json"


def test_explicit_openclaw_state_dir_env_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    custom = tmp_path / "custom-state"
    custom.mkdir()
    monkeypatch.setenv("OPENCLAW_STATE_DIR", str(custom))

    assert Path(adapter._openclaw_state_dir()) == custom
    assert Path(adapter._openclaw_config_path()) == custom / "openclaw.json"
    assert asserter.default_openclaw_json() == custom / "openclaw.json"
    cfg_file = custom / "openclaw.json"
    cfg_file.write_text(json.dumps({"mcp": {"servers": {"m": {"command": "echo"}}}}), encoding="utf-8")
    assert "m" in asserter.read_declared_servers(asserter.default_openclaw_json())
