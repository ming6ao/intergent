# Intergent + Claude Code

Claude Code speaks MCP natively, so it can call the Intergent engine directly.
There are two complementary pieces:

1. **MCP server** (`intergent mcp`) — exposes one `ig` tool for the unit
   lifecycle (`start`, `status`, `declare`, `commit`, `integrate`, `report`).
2. **Skill** (root [`SKILL.md`](../SKILL.md)) — teaches the campaign workflow and
   the rules. The repo is a self-contained skill; the CLI it calls is bundled.

## 1. Install the CLI

From the Intergent checkout:

```bash
pip install -e .        # or: pipx install .
intergent --version
```

## 2. Register the MCP server

Project-scoped (recommended, commit `.mcp.json` at the repo root):

```bash
cp integrations/claude/.mcp.json /path/to/repo/.mcp.json
claude mcp list          # should show "intergent"
```

Or add it for one machine:

```bash
claude mcp add intergent -- intergent mcp
```

Run Claude from the repository root: the server resolves the plane from its
working directory, and `--unit` is only needed when the cwd is not inside a unit
worktree.

## 3. Install the skill

The repository root **is** the Agent Skill (root `SKILL.md` + bundled
`bin/intergent`):

```bash
npx skills add ming6ao/intergent -g -y -a claude-code
```

Claude loads it on demand and will declare intent before editing, surface
`queued`/`needs_decision`, and stop at `commit`.

## 4. Workflow

```
coordinator: campaign start -> spawn -> verify -> integrate -> report
worker:      ig declare -> edit -> commit
```

A worker never runs `integrate`; the coordinator verifies each candidate and
lands it on the campaign feature branch. See
[`docs/orchestration.md`](../docs/orchestration.md).

## Available MCP tools

Exactly one tool, `ig`, with an `action` enum: `start`, `status`, `declare`,
`commit`, `integrate`, `report`. The schema is generated from
`intergent/surface.py`, so it matches the CLI exactly.
