# QuantDinger Agent Skill

[中文版](AGENT_SKILL_CN.md)

The QuantDinger Strategy Lab skill teaches coding agents (Claude Code, Codex, OpenClaw, and other clients that read `SKILL.md`) the order in which to use the QuantDinger MCP tools and where the safety boundaries are. A user describes an idea in one sentence, for example "design a BTC 15-minute ORB strategy and backtest it", and the agent:

1. confirms the environment with `check_environment()`;
2. confirms data coverage with `get_data_coverage()` and picks feasible in-sample and out-of-sample windows;
3. reads the authoring contract and templates, compiles with `compile_strategy_code()`, and saves the source with `save_strategy_source()`;
4. submits the backtest with `submit_backtest()` and waits with `wait_for_job()`;
5. diagnoses the result: cost share, whether win rate and payoff ratio break even, drawdown, and benchmark;
6. iterates one hypothesis at a time and keeps an iteration log.

The skill lives at [`skills/quantdinger-strategy-lab/SKILL.md`](skills/quantdinger-strategy-lab/SKILL.md). A complete example conversation, from the one-sentence request to the diagnosed result, is in [`references/example-session.md`](skills/quantdinger-strategy-lab/references/example-session.md).

## What the skill enforces

- **Safety:** research tokens only (R/W/B, paper-only, never T); no orders and no runtime changes; deployments start in `signal` mode and stay stopped; the agent never accepts terms or enters passwords, keys, or tokens on the user's behalf.
- **Overfitting:** no parameter sweeps aimed at a return target; parameter changes are confirmed on an untouched out-of-sample window (or walk-forward windows); every result reports the sample size, the market conditions of the test windows, and the cost assumptions, plus a doubled-cost check.

## Prerequisites

1. A running QuantDinger backend and a backtest-only Agent Token issued by a human user (scopes `R`, `W`, `B`; `paper_only: true`). See [Step 0 of the strategy iteration guide](AGENT_STRATEGY_ITERATION.md#step-0-issue-a-backtest-only-agent-token).
2. The `quantdinger-mcp` server installed and connected to the agent, as described in [MCP setup](MCP_SETUP.md). Without MCP, the skill's Gateway map lets an agent call `/api/agent/v1` over HTTP instead.

Keep the token in the client's environment or secret store. It never goes into the skill files, prompts, or the repository.

## Install

The skill is a folder: copy the whole `docs/agent/skills/quantdinger-strategy-lab` directory, including `references/`. Run the commands from a repository checkout; `qd_agent_xxx` is a placeholder for the token.

### Claude Code

Install the skill for your user (all projects) or for one project:

```bash
mkdir -p ~/.claude/skills
cp -R docs/agent/skills/quantdinger-strategy-lab ~/.claude/skills/
# or, for a single project:
mkdir -p .claude/skills
cp -R docs/agent/skills/quantdinger-strategy-lab .claude/skills/
```

Register the MCP server:

```bash
claude mcp add quantdinger \
  -e QUANTDINGER_BASE_URL=http://localhost:8888 \
  -e QUANTDINGER_AGENT_TOKEN=qd_agent_xxx \
  -- quantdinger-mcp
```

Start a new session. Claude Code loads the skill when a request matches its description; you can also invoke it with `/quantdinger-strategy-lab`.

### Codex

Copy the skill into the Codex skills directory:

```bash
mkdir -p ~/.codex/skills
cp -R docs/agent/skills/quantdinger-strategy-lab ~/.codex/skills/
```

Add the MCP server to `~/.codex/config.toml`:

```toml
[mcp_servers.quantdinger]
command = "quantdinger-mcp"
env = { QUANTDINGER_BASE_URL = "http://localhost:8888", QUANTDINGER_AGENT_TOKEN = "qd_agent_xxx" }
```

Restart Codex so it picks up the skill and the server. If your Codex version reads skills from another directory, copy the same folder there.

### OpenClaw

Copy the skill into the shared skills directory, or into the `skills/` folder of a single workspace:

```bash
mkdir -p ~/.openclaw/skills
cp -R docs/agent/skills/quantdinger-strategy-lab ~/.openclaw/skills/
```

Connect `quantdinger-mcp` with the same command and environment variables as above, following OpenClaw's MCP configuration, or let the agent use the Gateway map in the skill with `QUANTDINGER_AGENT_TOKEN` from its secret store. Start a new session so the skill is loaded.

## Keeping the skill aligned with the contract

The skill may only reference MCP tools and Gateway operations that exist. `scripts/check_agent_skill.py` verifies that every MCP tool call in the skill and these guides is a registered MCP tool using its real parameter names, that every `METHOD /path` exists in [agent-openapi.json](agent-openapi.json), and that the frontmatter is valid. It runs in CI through `backend_api_python/tests/test_agent_skill_contract.py`; run it locally from the repository root with:

```bash
python3 scripts/check_agent_skill.py
```

When an MCP tool is renamed or its parameters change, update the skill in the same change. When a dedicated walk-forward or out-of-sample tool ships, switch the skill's overfitting step to it.
