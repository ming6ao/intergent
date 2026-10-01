# Using Intergent inside a coding agent

Intergent is driven by a **coordinator** session that spawns planner, worker,
and verifier subagents around a campaign DAG; the `intergent` engine owns
isolation (worktrees), scope leases, verification, and integration. This page
covers wiring an agent to the engine. The workflow itself lives in the
[`SKILL.md`](../SKILL.md) and its specification in
[Orchestration](./orchestration.md).

```
┌──────────────────────────┐        ┌──────────────────────────────┐
│  coordinator session     │        │  Intergent local plane       │
│  (Claude Code / pi / …)  │──CLI──►│  service (single state owner)│
│                          │◄─MCP───│  worktrees · leases · DB     │
└───────────┬──────────────┘        └──────────────┬───────────────┘
            │ spawns one-shot                       │ verify + integrate
            ▼                                       ▼
   planner · worker(s) · verifier            feature branch (per campaign)
```

## Adapters

| Agent | Mechanism | Setup |
|---|---|---|
| **pi** | native tools + bundled skill | [`integrations/pi/`](../integrations/pi/README.md) |
| **Claude Code** | MCP + bundled skill | [`integrations/claude/`](../integrations/claude/README.md) |
| **Any CLI agent** | the bundled `intergent` CLI via the skill | [`SKILL.md`](../SKILL.md) |
| **MCP-capable agents** | `intergent mcp` (stdio) | this doc |

### pi

pi has no MCP. The repository is a **pi package** that ships the `ig` and
`campaign` tools, the skill, and the CLI:

```bash
pi install ./                                       # local checkout
# pi install git:github.com/ming6ao/intergent
# pi install npm:intergent
INTERGENT_AUTO_BOOTSTRAP=0 pi                       # launch the coordinator
```

### Claude Code

```bash
npx skills add ming6ao/intergent -g -y -a claude-code
cp integrations/claude/.mcp.json /path/to/repo/   # project MCP server
claude
```

Tools are then available as a single `ig` MCP call.

### Skill only (`npx skills`)

The repository is also a self-contained Agent Skill: the root `SKILL.md`
describes the workflow and the repo bundles the CLI it calls (`bin/intergent` +
the `intergent/` package). Install it for any Agent Skills harness:

```bash
npx skills add ming6ao/intergent -g -y -a claude-code -a pi
```

Target the agent(s) explicitly (`-a`); with `-y` the skills CLI also counts
PromptScript, which is project-only and fails global installation. The skill
runs the bundled CLI with `python3 <skill-dir>/bin/intergent`, so no separate
`pip install` is required.

## Agent roles

| Role | Bound to a unit? | Contract |
|---|---|---|
| **Coordinator** | no | owns the plan (`dag.json`), spawns agents, calls `integrate` after a pass, writes the report |
| **Planner** | no | reads the design, writes `dag.json` |
| **Worker** | yes (its worktree) | `declare` → edit → acceptance (CPU) → `commit` |
| **Verifier** | no (read-only) | runs acceptance (T0 then `tools/gpu.sh`), returns a verdict, never edits |

### Worker contract

1. `intergent status --short` must succeed — you are in your unit worktree.
2. `declare` before editing, scoping every file/symbol.
   - `granted` → edit.
   - `queued` → **exit immediately** and report the blocker; the coordinator
     serializes the node or re-plans. Never force a conflict.
   - `needs_decision` → stop; the coordinator re-plans.
3. Run the node's acceptance commands (CPU only; never the GPU).
4. `commit` and stop. Workers never run `integrate` or `git merge`.

### Coordinator

Use the `campaign` tool in pi, or the CLI: `start --no-unit --main <feature>
--base <base>`, then `spawn`/`verify`/`integrate` per ready node, a final
idempotent `integrate` sweep, and `report`. See
[Orchestration](./orchestration.md).

## MCP surface

`intergent mcp` speaks newline-delimited JSON-RPC over stdio and exposes a
**single** tool, `ig`, with an `action` enum (`start`, `status`, `declare`,
`commit`, `integrate`, `report`). It is generated from `intergent/surface.py`,
the same module the CLI renders, so the two surfaces cannot drift. `unit` is
optional on hot-loop calls; the server resolves it from its cwd.

## Process supervision

Campaign orchestration spawns one-shot subagents as **child processes of the
coordinator**; they are not detached and are not steerable. A coordinator crash
kills them, and a resumed campaign resets any `running` node to `pending`.
`agent start`-style supervision, tmux attachment, and long-lived background
sessions from the design are not implemented.

Prev: [Local implementation](./implementation.md) · Next: [Orchestration](./orchestration.md)
