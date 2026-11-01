#!/usr/bin/env python3
"""Check that the QuantDinger Agent skill only references existing tools.

The skill under ``docs/agent/skills/`` and its install guides write MCP tool
calls as ``tool_name(arg=...)`` and Gateway operations as ``METHOD /path``.
This script verifies, without third-party dependencies, that:

- every call outside ``python`` code fences names a registered MCP tool
  (``MCP_TOOL_NAMES`` in ``mcp_server``) and only uses that tool's
  keyword parameters;
- every ``METHOD /path`` exists in ``docs/agent/agent-openapi.json``;
- the skill frontmatter is valid and the core workflow tools are referenced.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILLS_DIR = ROOT / "docs" / "agent" / "skills"
SKILL_GUIDES = (
    ROOT / "docs" / "agent" / "AGENT_SKILL.md",
    ROOT / "docs" / "agent" / "AGENT_SKILL_CN.md",
)
SERVER_PATH = ROOT / "mcp_server" / "src" / "quantdinger_mcp" / "server.py"
OPENAPI_PATH = ROOT / "docs" / "agent" / "agent-openapi.json"
GATEWAY_PREFIX = "/api/agent/v1"

# Tools every step of the skill workflow depends on.
REQUIRED_WORKFLOW_TOOLS = frozenset({
    "check_environment",
    "get_data_coverage",
    "get_strategy_authoring_contract",
    "list_strategy_templates",
    "compile_strategy_code",
    "save_strategy_source",
    "submit_backtest",
    "wait_for_job",
})

FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})\s*([\w+-]*)")
CALL_RE = re.compile(r"(?<![\w.])([a-z][a-z0-9_]*)\(")
KWARG_RE = re.compile(r"(?<![\w.])([a-z_][a-z0-9_]*)\s*=(?!=)")
DOUBLE_QUOTED_RE = re.compile(r'"[^"\n]*"')
STRING_RE = re.compile(r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'')
OPERATION_RE = re.compile(r"\b(GET|POST|PATCH|PUT|DELETE)\s+(/[A-Za-z0-9_\-/{}]+)")
PATH_PARAM_RE = re.compile(r"\{[^}]+\}")
FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
PYTHON_FENCE_LANGUAGES = {"python", "py"}


@dataclass(frozen=True)
class Contract:
    tools: dict[str, frozenset[str]]
    operations: frozenset[tuple[str, str]]


def load_tool_signatures(server_path: Path = SERVER_PATH) -> dict[str, frozenset[str]]:
    """Return ``{tool_name: parameter names}`` for every registered MCP tool."""
    tree = ast.parse(server_path.read_text(encoding="utf-8"))
    names: tuple[str, ...] | None = None
    functions: dict[str, frozenset[str]] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "MCP_TOOL_NAMES"
            for target in node.targets
        ):
            names = tuple(ast.literal_eval(node.value))
        elif isinstance(node, ast.FunctionDef):
            args = node.args
            params = [arg.arg for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs)]
            functions[node.name] = frozenset(params)
    if names is None:
        raise ValueError(f"MCP_TOOL_NAMES is missing from {server_path}")
    missing = [name for name in names if name not in functions]
    if missing:
        raise ValueError("MCP_TOOL_NAMES lists undefined functions: " + ", ".join(missing))
    return {name: functions[name] for name in names}


def _normalize_path(path: str) -> str:
    path = path.rstrip("/") or "/"
    if not path.startswith(GATEWAY_PREFIX):
        path = GATEWAY_PREFIX + path
    return PATH_PARAM_RE.sub("{}", path)


def load_gateway_operations(openapi_path: Path = OPENAPI_PATH) -> frozenset[tuple[str, str]]:
    """Return the ``(METHOD, normalized path)`` pairs documented for the Gateway."""
    spec = json.loads(openapi_path.read_text(encoding="utf-8"))
    operations: set[tuple[str, str]] = set()
    for path, item in (spec.get("paths") or {}).items():
        for method in item:
            if method.lower() in {"get", "post", "patch", "put", "delete"}:
                operations.add((method.upper(), _normalize_path(path)))
    return frozenset(operations)


def load_contract() -> Contract:
    return Contract(tools=load_tool_signatures(), operations=load_gateway_operations())


def strip_python_fences(text: str) -> str:
    """Drop the bodies of python fences (strategy code, not tool calls)."""
    output: list[str] = []
    active: tuple[str, int, bool] | None = None
    for line in text.splitlines():
        match = FENCE_RE.match(line)
        if match:
            fence, language = match.group(1), match.group(2).lower()
            if active is None:
                active = (fence[0], len(fence), language in PYTHON_FENCE_LANGUAGES)
                output.append("")
                continue
            if fence[0] == active[0] and len(fence) >= active[1] and not language:
                active = None
                output.append("")
                continue
        output.append("" if active is not None and active[2] else line)
    return "\n".join(output)


def _call_arguments(text: str, open_index: int) -> str | None:
    depth = 0
    index = open_index
    while index < len(text):
        char = text[index]
        if char in "\"'":
            closing = text.find(char, index + 1)
            if closing < 0:
                return None
            index = closing
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
            if depth == 0:
                return text[open_index + 1 : index]
        index += 1
    return None


def _top_level_kwargs(arguments: str) -> list[str]:
    flat = STRING_RE.sub('""', arguments)
    names: list[str] = []
    depth = 0
    start = 0
    for index, char in enumerate(flat + ","):
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == "," and depth == 0:
            match = KWARG_RE.match(flat[start:index].strip())
            if match:
                names.append(match.group(1))
            start = index + 1
    return names


def find_tool_calls(text: str) -> list[tuple[str, list[str], int]]:
    """Return ``(name, keyword names, line number)`` for each call-like token."""
    # Blank single-line double-quoted strings (argument values, quoted prompts)
    # so call-like text inside them is not mistaken for a tool call.
    searchable = DOUBLE_QUOTED_RE.sub(
        lambda match: '"' + " " * (len(match.group(0)) - 2) + '"',
        strip_python_fences(text),
    )
    calls: list[tuple[str, list[str], int]] = []
    for match in CALL_RE.finditer(searchable):
        arguments = _call_arguments(searchable, match.end() - 1)
        line = searchable.count("\n", 0, match.start()) + 1
        calls.append((match.group(1), _top_level_kwargs(arguments or ""), line))
    return calls


def check_text(text: str, source: str, contract: Contract) -> list[str]:
    errors: list[str] = []
    for name, kwargs, line in find_tool_calls(text):
        params = contract.tools.get(name)
        if params is None:
            errors.append(f"{source}:{line}: unknown MCP tool {name}()")
            continue
        for kwarg in kwargs:
            if kwarg not in params:
                errors.append(f"{source}:{line}: {name}() has no parameter {kwarg!r}")
    for match in OPERATION_RE.finditer(strip_python_fences(text)):
        method, path = match.group(1), match.group(2)
        if (method, _normalize_path(path)) not in contract.operations:
            line = text.count("\n", 0, match.start()) + 1
            errors.append(f"{source}:{line}: {method} {path} is not in agent-openapi.json")
    return errors


def check_skill_file(path: Path, text: str, contract: Contract) -> list[str]:
    source = str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)
    errors: list[str] = []
    match = FRONTMATTER_RE.match(text)
    if not match:
        return [f"{source}: missing YAML frontmatter"]
    fields: dict[str, str] = {}
    for raw in match.group(1).splitlines():
        key, sep, value = raw.partition(":")
        if sep:
            fields[key.strip()] = value.strip()
    name = fields.get("name", "")
    if not SKILL_NAME_RE.match(name) or len(name) > 64:
        errors.append(f"{source}: frontmatter name must be kebab-case, at most 64 characters")
    if name != path.parent.name:
        errors.append(f"{source}: frontmatter name {name!r} must match its directory")
    description = fields.get("description", "")
    if not description or len(description) > 1024:
        errors.append(f"{source}: frontmatter description must be 1-1024 characters")
    referenced = {call for call, _kwargs, _line in find_tool_calls(text)}
    for tool in sorted(REQUIRED_WORKFLOW_TOOLS - referenced):
        errors.append(f"{source}: workflow tool {tool}() is not referenced")
    return errors


def validate(contract: Contract | None = None) -> list[str]:
    contract = contract or load_contract()
    errors: list[str] = []
    skill_files = sorted(SKILLS_DIR.glob("*/SKILL.md"))
    if not skill_files:
        return [f"no SKILL.md found under {SKILLS_DIR.relative_to(ROOT)}"]
    documents = sorted(SKILLS_DIR.rglob("*.md")) + [path for path in SKILL_GUIDES if path.exists()]
    for path in skill_files:
        errors.extend(check_skill_file(path, path.read_text(encoding="utf-8"), contract))
    for path in documents:
        errors.extend(check_text(path.read_text(encoding="utf-8"), str(path.relative_to(ROOT)), contract))
    return errors


def main() -> int:
    errors = validate()
    if errors:
        print("Agent skill validation failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print("Agent skill tool names, parameters, and Gateway paths match the current contract.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
