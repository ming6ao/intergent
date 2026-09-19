# Intergent + Claude Code

Claude Code speaks MCP natively, so it can call the Intergent local plane
directly. There are two complementary pieces:

1. **MCP server** (`intergent mcp`) — gives Claude tools for declare/verify/etc.
2. **Skill** (root [`SKILL.md`](../SKILL.md)) — teaches the workflow and the rules.
   The repo is a self-contained skill; the CLI it calls is bundled.

## 1. Install the CLI

From the Intergent checkout:

```bash
pip install -e .        # or: pipx install .
intergent --version     # ig --version
```

## 2. Register the MCP server

Run Claude Code **inside the unit worktree** you want it to work in. The server
resolves the unit from its working directory, so `unit` arguments are optional.

Project-scoped (recommended, commit `.mcp.json` at the repo root):

```bash
cp integrations/claude/.mcp.json /path/to/repo/.mcp.json
claude mcp list          # should show "intergent"
```

Or add it for one machine:

```bash
claude mcp add intergent -- intergent mcp
```

> If Claude Code is started at the repository root instead of a unit worktree,
> the tools still work but every call needs an explicit `unit`. Prefer starting
> Claude from a unit worktree:
>
> ```bash
> intergent start --name auth-fix --agent claude-code
> cd "$(intergent --json start | python3 -c 'import sys,json;print(json.load(sys.stdin)["worktree"])')"
> claude
> ```

## 3. Install the skill

The repository root **is** the Agent Skill (root `SKILL.md` + bundled
`bin/intergent`). Install it with the skills CLI:

```bash
npx skills add ming6ao/intergent -g -y -a claude-code
```

Claude loads it on demand and will declare intent before editing, surface
`queued`/`needs_decision`, and stop before approval.

## 4. Workflow

```
agent: declare -> edit -> commit -> verify -> review
human: intergent review <candidate> --approve ; intergent review --land --all
       # or in one step: intergent submit <candidate>
```

The MCP surface deliberately excludes landing; `submit` and the `review`
approval flags are human actions.

## Available MCP tools

Exactly one tool, `ig`, with an `action` enum: `start`, `status`, `declare`,
`commit`, `verify`, `review`. The schema is generated from
`intergent/surface.py`, so it matches the CLI exactly. Human actions are absent.
