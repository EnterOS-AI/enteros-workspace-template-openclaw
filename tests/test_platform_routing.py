"""Unit tests for adapter.resolve_platform_routing — the platform-managed
LLM routing override.

In platform_managed billing the tenant has no BYOK key (the workspace-server
strips them), so the per-vendor colon registry (resolve_provider_routing)
would raise "No API key found". resolve_platform_routing short-circuits that:
route everything through the Molecule proxy's OpenAI-compat surface.

adapter.py is loaded by tests/conftest.py straight from the repo root; its
top-level imports require molecule_runtime. If that's unavailable the module
isn't in sys.modules and these tests skip (mirrors test_model_fallback.py).
"""
from __future__ import annotations

import sys

import pytest

adapter = sys.modules.get("adapter")
if adapter is None:
    pytest.skip(
        "adapter module unavailable (molecule_runtime not installed)",
        allow_module_level=True,
    )

PROXY = "https://api.moleculesai.app/api/v1/internal/llm/openai/v1"


def test_not_platform_managed_returns_none():
    # byok / unset -> no override; the colon registry path runs as before.
    assert adapter.resolve_platform_routing("minimax:MiniMax-M2.7", {}) is None
    assert adapter.resolve_platform_routing(
        "moonshot:kimi-k2.6", {"MOLECULE_LLM_BILLING_MODE": "byok"}
    ) is None


def test_platform_routes_to_proxy_openai_surface():
    env = {
        "MOLECULE_LLM_BILLING_MODE": "platform_managed",
        "MOLECULE_LLM_BASE_URL": PROXY,
        "MOLECULE_LLM_USAGE_TOKEN": "tok-123",
    }
    key, url, model, compat = adapter.resolve_platform_routing("moonshot/kimi-k2.6", env)
    assert (key, url, model, compat) == ("tok-123", PROXY, "moonshot/kimi-k2.6", "openai")


def test_platform_prefix_is_stripped():
    env = {
        "MOLECULE_LLM_BILLING_MODE": "platform_managed",
        "MOLECULE_LLM_BASE_URL": PROXY,
        "MOLECULE_LLM_USAGE_TOKEN": "t",
    }
    _, _, model, _ = adapter.resolve_platform_routing("platform/moonshot/kimi-k2.6", env)
    assert model == "moonshot/kimi-k2.6"


def test_empty_model_falls_back_to_platform_default():
    env = {
        "MOLECULE_LLM_BILLING_MODE": "platform_managed",
        "MOLECULE_LLM_BASE_URL": PROXY,
        "MOLECULE_LLM_USAGE_TOKEN": "t",
    }
    _, _, model, _ = adapter.resolve_platform_routing("", env)
    assert model == adapter.OPENCLAW_PLATFORM_DEFAULT_MODEL


def test_openai_base_url_and_anthropic_token_fallbacks():
    env = {
        "MOLECULE_LLM_BILLING_MODE": "platform_managed",
        "OPENAI_BASE_URL": PROXY,
        "ANTHROPIC_API_KEY": "sk-ant-xx",
    }
    key, url, _, _ = adapter.resolve_platform_routing("moonshot/kimi-k2.6", env)
    assert key == "sk-ant-xx" and url == PROXY


def test_fail_closed_when_platform_unconfigured():
    # platform_managed but no base/token -> raise, do NOT fall back to a
    # keyless BYOK route.
    with pytest.raises(RuntimeError):
        adapter.resolve_platform_routing(
            "moonshot/kimi-k2.6", {"MOLECULE_LLM_BILLING_MODE": "platform_managed"}
        )
