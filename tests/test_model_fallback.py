"""Unit tests for coerce_servable_model — the fresh-provision boot fix.

Root cause being pinned here (diagnosed 2026-05-16 from a real fresh
prod openclaw provision `cdae52b2` on the correctly-pinned image
sha256:cd6e75d…):

    WARNING: adapter.setup() failed — Reason: RuntimeError: No API key
    found for provider 'anthropic' (checked: OPENAI_API_KEY).

A fresh openclaw provision gets a stub /configs/config.yaml with no
`model:` and no MODEL env, so the shared molecule-runtime load_config
defaults the model to `anthropic:claude-opus-4-7`. `anthropic` is not
in OPENCLAW_PROVIDERS, so resolve_provider_routing falls back to
OPENAI_API_KEY, finds none, raises, and setup() aborts before the
gateway starts. coerce_servable_model() is the guard that prevents an
unroutable provider from ever being the effective default.

These tests import adapter.py the same way the runtime / canonical
validator does (top-level import). adapter.py's only heavy import is
molecule_runtime; if that's unavailable the whole module import fails
and these tests are skipped (mirrors the validator's soft-skip).
"""
from __future__ import annotations

import sys

import pytest

# tests/conftest.py pre-loads adapter.py as the top-level `adapter`
# module straight from its file path (the same way molecule-runtime /
# the canonical validator load it). If its only heavy dep
# (molecule_runtime) is unavailable the load is skipped and `adapter`
# is absent — soft-skip then, mirroring the validator.
if "adapter" not in sys.modules:
    pytest.skip(
        "adapter.py not importable (molecule_runtime missing) — "
        "matches canonical validator soft-skip",
        allow_module_level=True,
    )
adapter = sys.modules["adapter"]


class TestCoerceServableModel:
    def test_unroutable_anthropic_default_falls_back_to_template_default(self):
        """The exact prod failure: load_config's anthropic default must
        be coerced to a model OpenClaw can actually route."""
        out = adapter.coerce_servable_model("anthropic:claude-opus-4-7")
        assert out == adapter.OPENCLAW_DEFAULT_MODEL
        assert out == "openai:gpt-4.1-mini"
        # And the result must itself be routable by the registry.
        assert out.split(":", 1)[0] in adapter.OPENCLAW_PROVIDERS

    @pytest.mark.parametrize(
        "model",
        [
            "openai:gpt-4.1-mini",
            "openai:gpt-4o",
            "groq:llama-3.3-70b-versatile",
            "openrouter:anthropic/claude-sonnet-4-5",
            "qianfan:ernie-4.0",
            "minimax:MiniMax-M2.7-highspeed",
            "moonshot:kimi-k2.6",
        ],
    )
    def test_registry_models_pass_through_untouched(self, model):
        """An operator who explicitly picks an OpenClaw-supported
        provider must be unaffected — including the MiniMax/Kimi paths
        Hongming's hot-patched tenant relies on."""
        assert adapter.coerce_servable_model(model) == model

    def test_bare_model_id_passes_through(self):
        """No provider prefix → resolve_provider_routing treats it as
        openai:* which IS routable; don't rewrite it."""
        assert adapter.coerce_servable_model("gpt-4o-mini") == "gpt-4o-mini"

    def test_other_unroutable_providers_also_coerced(self):
        """Any provider not in the registry (not just anthropic) must
        coerce — e.g. a future generic default change upstream."""
        assert (
            adapter.coerce_servable_model("gemini:gemini-2.5-flash")
            == adapter.OPENCLAW_DEFAULT_MODEL
        )
        assert (
            adapter.coerce_servable_model("vertex:claude-3-5")
            == adapter.OPENCLAW_DEFAULT_MODEL
        )

    def test_default_model_is_self_consistent(self):
        """Guard against a future edit pointing OPENCLAW_DEFAULT_MODEL
        at a provider the registry can't route — that would reintroduce
        the exact bug for every keyless fresh provision."""
        prefix = adapter.OPENCLAW_DEFAULT_MODEL.split(":", 1)[0]
        assert prefix in adapter.OPENCLAW_PROVIDERS
