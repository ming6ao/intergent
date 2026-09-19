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
> intergent workspace create auth-fix --agent claude-code
> cd "$(intergent --json workspace current | python3 -c 'import sys,json;print(json.load(sys.stdin)["worktree"])')"
> claude
> ```

## 3. Install the skill

The repository root **is** the Agent Skill (root `SKILL.md` + bundled
`bin/intergent`). Install it with the skills CLI:

```bash
npx skills add <owner>/intergent -a claude-code -g -y
# or, from a local checkout:
npx skills add . -a claude-code -g -y
```

Alternatively symlink the repo root into your project skills directory:

```bash
mkdir -p .claude/skills
ln -s /path/to/intergent .claude/skills/intergent
```

Claude loads it on demand and will declare intent before editing, surface
`queued`/`needs_decision`, and stop before approval.

## 4. Workflow

```
agent: declare_intent -> edit -> commit_workspace -> finish_workspace -> verify -> submit
human: intergent review <candidate> ; intergent approve <candidate> ; intergent land --all
```

The MCP surface deliberately excludes `approve`/`land`; landing is a human act.

## Available MCP tools

`register_agent`, `create_workspace`, `register_child`, `declare_intent`,
`check_conflicts`, `claim_scope`, `heartbeat`, `release`, `commit_workspace`,
`finish_workspace`, `verify`, `status`, `current_workspace`, `submit`.
