"""The QuantDinger Agent skill must only reference tools that exist."""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = REPO_ROOT / "docs" / "agent" / "skills" / "quantdinger-strategy-lab"


def _load_checker():
    spec = importlib.util.spec_from_file_location(
        "check_agent_skill", REPO_ROOT / "scripts" / "check_agent_skill.py"
    )
    module = importlib.util.module_from_spec(spec)
    # Dataclasses resolve their module through sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


checker = _load_checker()


@pytest.fixture(scope="module")
def contract():
    return checker.load_contract()


def test_repository_skill_matches_current_contract(contract):
    assert checker.validate(contract) == []


def test_skill_and_guides_are_published():
    assert (SKILL_DIR / "SKILL.md").is_file()
    assert (SKILL_DIR / "references" / "example-session.md").is_file()
    for guide in checker.SKILL_GUIDES:
        assert guide.is_file(), guide
        assert "skills/quantdinger-strategy-lab" in guide.read_text(encoding="utf-8")
    for index in ("README.md", "README_CN.md"):
        text = (REPO_ROOT / "docs" / "agent" / index).read_text(encoding="utf-8")
        assert "AGENT_SKILL" in text, index


def test_contract_reflects_mcp_signatures(contract):
    assert contract.tools["submit_backtest"] >= {"code", "start_date", "end_date", "idempotency_key"}
    assert ("GET", "/api/agent/v1/data-coverage") in contract.operations


def test_unknown_tool_and_parameter_are_reported(contract):
    text = (
        "Call `run_backtest(code=...)` and then "
        "`submit_backtest(code=\"x\", start=\"2025-01-01\", params={\"a\": 1})`."
    )
    errors = checker.check_text(text, "sample.md", contract)
    assert any("unknown MCP tool run_backtest()" in error for error in errors)
    assert any("submit_backtest() has no parameter 'start'" in error for error in errors)
    assert not any("'a'" in error for error in errors)


def test_strings_and_nested_arguments_do_not_produce_false_parameters(contract):
    text = (
        'save_strategy_source(name="x", code="a = b(c=1)", '
        'metadata={"k": 1}, idempotency_key="test-key")'
    )
    assert checker.check_text(text, "sample.md", contract) == []


def test_python_fences_are_ignored_but_other_fences_are_checked(contract):
    text = (
        "```python\ncontext.set_warmup(96)\norder_target_percent(g.symbol, 0.5)\n```\n"
        "```text\nmade_up_tool()\n```\n"
    )
    errors = checker.check_text(text, "sample.md", contract)
    assert errors == ["sample.md:6: unknown MCP tool made_up_tool()"]


def test_unknown_gateway_operation_is_reported(contract):
    errors = checker.check_text(
        "Use `GET /environment` and `POST /backtest/submit`.", "sample.md", contract
    )
    assert errors == ["sample.md:1: POST /backtest/submit is not in agent-openapi.json"]


def test_skill_frontmatter_and_required_tools(contract):
    path = SKILL_DIR / "SKILL.md"
    errors = checker.check_skill_file(path, "---\nname: Wrong Name\n---\nbody\n", contract)
    assert any("kebab-case" in error for error in errors)
    assert any("must match its directory" in error for error in errors)
    assert any("description" in error for error in errors)
    assert any("check_environment()" in error for error in errors)
    assert checker.check_skill_file(path, "no frontmatter", contract)[0].endswith(
        "missing YAML frontmatter"
    )


def _example_strategy_source() -> str:
    text = (SKILL_DIR / "references" / "example-session.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```python\n(.*?)\n```", text, re.S)
    assert len(blocks) == 1
    return blocks[0]


def test_example_strategy_passes_static_contract_checks():
    from app.services.strategy_v2 import contract as strategy_contract
    from app.utils.safe_exec import validate_code_safety

    code = _example_strategy_source()
    is_safe, error = validate_code_safety(code)
    assert is_safe, error
    strategy_contract._validate_dataframe_truthiness(code)
    strategy_contract._validate_strategy_api_calls(code)
    strategy_contract._validate_safe_module_top_level(code)


def test_example_strategy_compiles_to_the_manifest_shown_in_the_session():
    from app.services.strategy_v2.contract import compile_strategy_v2

    manifest = compile_strategy_v2(_example_strategy_source()).manifest.metadata()
    assert manifest["warmupBars"] == 96
    assert manifest["drivingFrequency"] == "15m"
    assert manifest["frequencies"] == ["15m"]
    assert manifest["directionMode"] == "long_only"
    assert manifest["leverageAllowed"] is False


def test_skill_states_safety_and_overfitting_rules():
    text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    for phrase in (
        "Research scopes only",
        "No orders",
        'execution_mode="signal"',
        "never enter or accept them",
        "No parameter sweeps",
        "out-of-sample",
        "Sample size",
        "Market conditions",
        "Cost assumptions",
    ):
        assert phrase in text, phrase
