"""Unit tests for the consult_committee plugin handler.

The handler imports Hermes-internal modules lazily inside the function body,
so we mock those imports at the module level via sys.modules before each test
that reaches the fan-out path. This keeps the tests fully self-contained —
no live LLM calls, no Hermes installation required.
"""
from __future__ import annotations

import importlib
import json
import sys
import types

import pytest

# ---------------------------------------------------------------------------
# Load the plugin as a standalone module (it has no Hermes import-time deps)
# ---------------------------------------------------------------------------
_PLUGIN_PATH = __import__("os").path.join(
    __import__("os").path.dirname(__import__("os").path.dirname(__file__)),
    "__init__.py",
)
_spec = importlib.util.spec_from_file_location(
    "consult_committee", _PLUGIN_PATH
)
consult_committee = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(consult_committee)

_handle = consult_committee._handle_consult_committee


# ---------------------------------------------------------------------------
# Helpers: build a fake Hermes-internal module bundle
# ---------------------------------------------------------------------------
def _install_fake_hermes_modules(
    *,
    preset_models=None,
    run_outputs=None,
    run_side_effect=None,
):
    """Inject mock Hermes modules into sys.modules and return the run mock."""
    # tools.registry
    def _tool_result(payload):
        return json.dumps(payload)

    def _tool_error(msg):
        return json.dumps({"error": msg})

    tools_mod = types.ModuleType("tools")
    registry_mod = types.ModuleType("tools.registry")
    registry_mod.tool_result = _tool_result
    registry_mod.tool_error = _tool_error
    tools_mod.registry = registry_mod
    sys.modules["tools"] = tools_mod
    sys.modules["tools.registry"] = registry_mod

    # hermes_cli.config
    config_mod = types.ModuleType("hermes_cli.config")
    moa_config_dict = {"default": {"reference_models": preset_models or []}}
    config_mod.load_config = lambda: {"moa": moa_config_dict}
    hermes_cli_mod = types.ModuleType("hermes_cli")
    hermes_cli_mod.config = config_mod
    sys.modules["hermes_cli"] = hermes_cli_mod
    sys.modules["hermes_cli.config"] = config_mod

    # hermes_cli.moa_config
    moa_config_mod = types.ModuleType("hermes_cli.moa_config")

    def _resolve(cfg, name):
        if name not in cfg:
            raise KeyError(name)
        return cfg[name]

    moa_config_mod.resolve_moa_preset = _resolve
    hermes_cli_mod.moa_config = moa_config_mod
    sys.modules["hermes_cli.moa_config"] = moa_config_mod

    # agent.moa_loop
    agent_mod = types.ModuleType("agent")
    moa_loop_mod = types.ModuleType("agent.moa_loop")

    if run_side_effect is not None:
        run_mock = run_side_effect
    else:
        run_mock = lambda *a, **kw: run_outputs
    moa_loop_mod._run_references_parallel = run_mock
    agent_mod.moa_loop = moa_loop_mod
    sys.modules["agent"] = agent_mod
    sys.modules["agent.moa_loop"] = moa_loop_mod

    return run_mock


@pytest.fixture(autouse=True)
def _reset_escalation_counter():
    """Reset the module-level escalation counter between tests."""
    consult_committee._escalation_count = 0
    yield
    consult_committee._escalation_count = 0


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
class TestThreeTupleUnpacking:
    """The core fix: _run_references_parallel returns 3-tuples (label, text, accounting)."""

    def test_three_tuples_unpacked_correctly(self):
        run_mock = _install_fake_hermes_modules(
            preset_models=[{"model": "claude-sonnet-4"}],
            run_outputs=[
                ("claude-sonnet-4", "The answer is 42.", {"tokens": 100}),
                ("gpt-5.5", "I agree, 42.", {"tokens": 80}),
            ],
        )
        result = _handle({"question": "What is 6 × 7?"})
        payload = json.loads(result)

        assert "advisors" in payload
        assert payload["answered"] == 2
        assert payload["failed"] == 0
        assert len(payload["advisors"]) == 2
        assert payload["advisors"][0]["model"] == "claude-sonnet-4"
        assert payload["advisors"][0]["answer"] == "The answer is 42."
        assert payload["advisors"][0]["failed"] is False
        # The accounting dict was discarded by the handler and must NOT appear
        assert "accounting" not in payload["advisors"][0]

    def test_mixed_success_and_failure(self):
        _install_fake_hermes_modules(
            preset_models=[{"model": "m1"}, {"model": "m2"}, {"model": "m3"}],
            run_outputs=[
                ("m1", "Good answer", {"tokens": 10}),
                ("m2", "[error] auth failed", {"tokens": 0}),
                ("m3", "", {"tokens": 0}),
            ],
        )
        result = _handle({"question": "hard question"})
        payload = json.loads(result)

        assert payload["answered"] == 1
        assert payload["failed"] == 2
        assert payload["advisors"][0]["failed"] is False
        assert payload["advisors"][1]["failed"] is True
        assert payload["advisors"][2]["failed"] is True


class TestBudgetCap:
    def test_budget_exhausted_after_max_escalations(self):
        _install_fake_hermes_modules(
            preset_models=[{"model": "m1"}],
            run_outputs=[("m1", "answer", {})],
        )

        # Use up all 5 allowed escalations
        for _ in range(consult_committee._MAX_ESCALATIONS):
            r = _handle({"question": "q"})
            assert "error" not in json.loads(r)

        # 6th call should be rejected
        result = json.loads(_handle({"question": "q"}))
        assert "error" in result
        assert "budget exhausted" in result["error"]

    def test_budget_counter_is_per_process(self):
        """The counter is module-level, not per-call — verify it increments."""
        _install_fake_hermes_modules(
            preset_models=[{"model": "m1"}],
            run_outputs=[("m1", "answer", {})],
        )
        assert consult_committee._escalation_count == 0
        _handle({"question": "q"})
        assert consult_committee._escalation_count == 1
        _handle({"question": "q"})
        assert consult_committee._escalation_count == 2


class TestValidation:
    def test_empty_question_returns_error(self):
        # No need to install Hermes modules — validation happens first
        result = json.loads(_handle({"question": ""}))
        assert "error" in result
        assert "question is required" in result["error"]

    def test_missing_question_returns_error(self):
        result = json.loads(_handle({}))
        assert "error" in result
        assert "question is required" in result["error"]

    def test_unknown_preset_returns_error(self):
        _install_fake_hermes_modules(preset_models=[{"model": "m1"}])
        result = json.loads(_handle({"question": "q", "preset": "nonexistent"}))
        assert "error" in result
        assert "unknown MoA preset" in result["error"]

    def test_empty_reference_models_returns_error(self):
        _install_fake_hermes_modules(preset_models=[])
        result = json.loads(_handle({"question": "q"}))
        assert "error" in result
        assert "no reference models" in result["error"]


class TestAllAdvisorsFail:
    def test_all_failed_returns_error(self):
        _install_fake_hermes_modules(
            preset_models=[{"model": "m1"}, {"model": "m2"}],
            run_outputs=[
                ("m1", "[error] timeout", {"tokens": 0}),
                ("m2", "failed: quota exceeded", {"tokens": 0}),
            ],
        )
        result = json.loads(_handle({"question": "q"}))
        assert "error" in result
        assert "all committee advisors failed" in result["error"]


class TestTruncation:
    def test_long_answer_truncated(self):
        long_text = "x" * (consult_committee._TRUNCATE_CHARS + 500)
        _install_fake_hermes_modules(
            preset_models=[{"model": "m1"}],
            run_outputs=[("m1", long_text, {"tokens": 9999})],
        )
        result = json.loads(_handle({"question": "q"}))
        answer = result["advisors"][0]["answer"]

        assert len(answer) <= consult_committee._TRUNCATE_CHARS + len("\n[... truncated]")
        assert answer.endswith("[... truncated]")


class TestExceptionHandling:
    def test_handler_never_raises(self):
        """The handler must catch all exceptions and return tool_error, never raise."""
        def boom(*a, **kw):
            raise RuntimeError("simulated LLM provider outage")

        _install_fake_hermes_modules(
            preset_models=[{"model": "m1"}],
            run_side_effect=boom,
        )
        result = json.loads(_handle({"question": "q"}))
        assert "error" in result
        assert "committee consultation failed" in result["error"]
