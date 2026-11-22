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


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits only")
def test_path_has_unsafe_ancestor_detects_non_sticky_writable(tmp_path):
    unsafe_dir = tmp_path / "oc"
    unsafe_dir.mkdir()
    os.chmod(unsafe_dir, 0o777)
    child = unsafe_dir / "state"
    child.mkdir(mode=0o700)
    assert adapter._path_has_unsafe_ancestor(str(child)) is True

    sticky_dir = tmp_path / "sticky"
    sticky_dir.mkdir()
    os.chmod(sticky_dir, 0o1777)
    sticky_child = sticky_dir / "logs"
    sticky_child.mkdir(mode=0o700)
    assert adapter._path_has_unsafe_ancestor(str(sticky_child)) is False

    safe_dir = tmp_path / "safe"
    safe_dir.mkdir(mode=0o700)
    safe_child = safe_dir / "state"
    safe_child.mkdir(mode=0o700)
    assert adapter._path_has_unsafe_ancestor(str(safe_child)) is False


def test_ensure_safe_openclaw_logging_redirects_unsafe_and_preserves_safe(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    state_dir = tmp_path / ".openclaw" / "state"
    state_dir.mkdir(parents=True)
    safe_log = tmp_path / "custom" / "openclaw.log"

    # Unsafe state_dir ancestor -> sets logging.file under ~/.cache/openclaw/logs
    monkeypatch.setattr(
        adapter,
        "_path_has_unsafe_ancestor",
        lambda p: str(state_dir) in os.path.abspath(p),
    )
    cfg: dict = {}
    assert adapter._ensure_safe_openclaw_logging(cfg, str(state_dir)) is True
    assert cfg["logging"]["file"].endswith(os.path.join(".cache", "openclaw", "logs", "openclaw.log"))

    # Preserves explicitly configured safe logging.file and creates parent dir
    cfg2 = {"logging": {"file": str(safe_log)}}
    assert adapter._ensure_safe_openclaw_logging(cfg2, str(state_dir)) is False
    assert cfg2["logging"]["file"] == str(safe_log)
    assert safe_log.parent.is_dir()

    # No-op when state_dir has no unsafe ancestors
    monkeypatch.setattr(adapter, "_path_has_unsafe_ancestor", lambda _p: False)
    cfg3: dict = {}
    assert adapter._ensure_safe_openclaw_logging(cfg3, str(state_dir)) is False
    assert "logging" not in cfg3


def test_openclaw_node_options_and_cli_env_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("OPENCLAW_STATE_DIR", raising=False)
    monkeypatch.delenv("NODE_OPTIONS", raising=False)

    assert adapter._openclaw_node_options("") == "--max-old-space-size=512 --max-semi-space-size=2"
    assert (
        adapter._openclaw_node_options("--trace-warnings --max-old-space-size=768")
        == "--trace-warnings --max-old-space-size=768 --max-semi-space-size=2"
    )
    assert (
        adapter._openclaw_node_options("--max-semi-space-size 4 --max-old-space-size=1024")
        == "--max-semi-space-size 4 --max-old-space-size=1024"
    )

    env = adapter._openclaw_cli_env({"EXTRA": "yes"})
    assert "--max-old-space-size=512" in env["NODE_OPTIONS"]
    assert "--max-semi-space-size=2" in env["NODE_OPTIONS"]
    assert env["EXTRA"] == "yes"


def test_probe_gateway_ready_states(monkeypatch):
    import io
    import urllib.error
    import urllib.request

    class FakeResp:
        def __init__(self, body: str):
            self._body = body.encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return self._body

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=5: FakeResp('{"ready":true,"coreReady":true}'))
    assert adapter._probe_gateway_ready(18789)[0] == "ready"

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=5: FakeResp('{"ready":false,"coreReady":false}'))
    assert adapter._probe_gateway_ready(18789)[0] == "pending"

    def raise_503(req, timeout=5):
        raise urllib.error.HTTPError(req.full_url, 503, "starting", {}, io.BytesIO(b'{"ready":false}'))

    monkeypatch.setattr(urllib.request, "urlopen", raise_503)
    assert adapter._probe_gateway_ready(18789)[0] == "pending"

    def raise_404(req, timeout=5):
        raise urllib.error.HTTPError(req.full_url, 404, "not found", {}, io.BytesIO(b""))

    monkeypatch.setattr(urllib.request, "urlopen", raise_404)
    assert adapter._probe_gateway_ready(18789)[0] == "unsupported"

    def raise_conn(req, timeout=5):
        raise ConnectionRefusedError("refused")

    monkeypatch.setattr(urllib.request, "urlopen", raise_conn)
    assert adapter._probe_gateway_ready(18789)[0] == "unreachable"
