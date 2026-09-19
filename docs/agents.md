# Using Intergent inside a coding agent

Intergent integrates with a coding agent (Claude Code, pi, Codex, Cursor, …)
the same way it integrates with a human: the agent drives the `intergent` CLI or
the MCP server, and the human keeps approval and landing.

```
┌──────────────────────────┐        ┌──────────────────────────────┐
│  coding agent session    │        │  Intergent local plane       │
│  (Claude Code / pi / …)  │──CLI──►│  service (single state owner)│
│  cwd = its unit worktree │◄─MCP───│  git worktrees · leases · DB │
└──────────────────────────┘        └──────────────┬───────────────┘
                                                   │ user approves
                                                   ▼
                                            local main branch
```

## The one rule that makes it work

**Run the agent inside its own unit worktree.** Intergent gives each session a
`git worktree` + branch (`ig/<session>/<unit>`). The agent's working directory
must be that worktree, so its edits, commits, and the tools' cwd all agree.
The CLI and MCP server resolve the unit from the cwd (`intergent workspace
current`), so no `--unit` bookkeeping is needed.

```bash
intergent workspace create auth-fix --agent claude-code
cd "$(intergent --json workspace current | python3 -c 'import sys,json;print(json.load(sys.stdin)["worktree"])')"
# now launch the agent here
```

This is exactly the design's "one tmux pane / session per unit". Different
sessions get different worktrees; scope leases stop them authoring conflicts.

## Install as a skill (`npx skills`)

This repository is a self-contained Agent Skill: the root
[`SKILL.md`](../SKILL.md) describes the workflow and the repo ships the CLI it
calls (`bin/intergent` + the `intergent/` Python package). Install it globally
for the agents you use:

```bash
npx skills add ming6ao/intergent -g -y -a claude-code -a pi
```

Target the agent(s) explicitly (`-a`) rather than relying on detection: with
`-y` the `skills` CLI also counts PromptScript, which is project-only, and prints
a ``PromptScript does not support global skill installation`` failure. Naming
the agents skips that path and installs cleanly.

Because the whole repo is installed as the skill, `bin/intergent` and the
`intergent/` package are present in the installed directory. The skill runs the
bundled CLI with `python3 <skill-dir>/bin/intergent`, so no separate
`pip install` is required.

> Discovery note: the `skills` CLI returns the root `SKILL.md` immediately and
> will not descend into subdirectories unless you pass `--full-depth`. Keep the
> root [`SKILL.md`](../SKILL.md) as the single source of truth for the skill.

## Adapters

| Agent | Mechanism | Setup |
|---|---|---|
| **Claude Code** | MCP (native) + bundled skill | [`integrations/claude/`](../integrations/claude/README.md) |
| **pi** | extension (no MCP) + bundled skill | [`integrations/pi/`](../integrations/pi/README.md) |
| **Any CLI agent** | the bundled `intergent` CLI via the skill | [`SKILL.md`](../SKILL.md) |
| **MCP-capable agents** | `intergent mcp` (stdio) | this doc |

### Claude Code

```bash
npx skills add ming6ao/intergent -g -y -a claude-code
cp integrations/claude/.mcp.json /path/to/repo/   # project MCP server
cd <unit-worktree> && claude
```

Tools are then available as MCP calls. See
[integrations/claude/README.md](../integrations/claude/README.md).

### pi

pi intentionally has no MCP. Install the extension that exposes `ig_*` tools,
and/or the bundled skill:

```bash
npx skills add ming6ao/intergent -g -y -a pi
cp integrations/pi/intergent.ts ~/.pi/agent/extensions/intergent.ts
cd <unit-worktree> && pi
```

See [integrations/pi/README.md](../integrations/pi/README.md).

### Generic CLI agent

Point the agent at the root [`SKILL.md`](../SKILL.md) (Agent Skills standard,
readable by Claude Code, pi, Codex, Cursor, and other harnesses). If the agent
only reads a repository instruction file, add the workflow to `AGENTS.md` /
`CLAUDE.md`:

```markdown
This repo uses Intergent. Before editing, run `intergent declare` in your unit
worktree. Never run `intergent approve` or `intergent land`.
```

## Agent contract

The tools/skill enforce this contract:

1. `workspace current` must succeed — you are in a unit worktree.
2. `declare` before editing. Handle the response:
   - `granted` → edit;
   - `queued` → do other work or wait + heartbeat, then rebase;
   - `needs_decision` → **stop and ask the human** (wait / redesign / override).
3. `commit` → `finish` → `verify` the exact commit.
4. `review` and report the candidate to the human.
5. **Never `approve` or `land`.** Those are human-only. The MCP and pi tool
   surfaces deliberately omit them.

## MCP surface

`intergent mcp` speaks newline-delimited JSON-RPC over stdio and exposes:
`register_agent`, `create_workspace`, `register_child`, `declare_intent`,
`check_conflicts`, `claim_scope`, `heartbeat`, `release`, `commit_workspace`,
`finish_workspace`, `verify`, `status`, `current_workspace`, `submit`.

`unit` is optional on the hot-loop tools; the server resolves it from its cwd.
`submit` returns the review packet and never lands.

## Multiple sessions at once

```bash
# session A
intergent workspace create payments --agent claude-code
# session B
intergent workspace create docs --agent pi

# later, human reviews and lands approved candidates in wave order
intergent status
intergent review <candidate>
intergent approve <candidate>
intergent land --all
```

`agent start`-style process supervision, tmux attachment, and background
sessions from the design are not implemented yet; today you launch each agent in
its unit worktree yourself.

Prev: [Local implementation](./implementation.md) · Next: [Remote plane](./remote-plane.md)
