# Intergent + pi

pi has **no built-in MCP** by design. It recommends either CLI tools documented
by a skill, or an extension that adds the tools. This directory provides both.

## Recommended: the extension (one `ig` tool)

The extension `intergent.ts` registers a **single** native tool, `ig`,
parameterized by an `action` enum — the same action surface as the CLI and MCP
server (`intergent/surface.py`). It is a thin forwarder to the `intergent` CLI
and is cwd-native: once a session is bound to a unit worktree, every call runs
there.

1. Install the CLI so `intergent` is on `PATH`:

   ```bash
   pip install -e /path/to/intergent    # or set INTERGENT_BIN
   ```

2. Install the extension:

   ```bash
   # global (all projects)
   cp integrations/pi/intergent.ts ~/.pi/agent/extensions/intergent.ts
   # or project-local
   mkdir -p .pi/extensions && cp integrations/pi/intergent.ts .pi/extensions/
   ```

3. Start pi **anywhere in the repository** and reload (`/reload` if already
   running).

   **Bootstrapping is tag-gated.** On the first prompt that mentions Intergent
   (e.g. `intergent: add a scope check to Login`), the extension runs
   `intergent start --agent pi` (idempotent plane + unit), binds its tools to the
   returned worktree, and injects the required lifecycle into the turn. Set
   `INTERGENT_AUTO_BOOTSTRAP=1` to bootstrap on every session start, or `=0` to
   disable. pi cannot change a live session's cwd, so the built-in
   `read`/`bash`/`edit`/`write`/`grep`/`find`/`ls` tools are rebound to the unit
   worktree.

If the binary is not on `PATH`, set `INTERGENT_BIN` in the environment before
launching pi, e.g. `INTERGENT_BIN=/home/me/intergent/bin/intergent pi`.

### `ig` actions

| Action | Purpose |
|---|---|
| `start` | bootstrap the plane + a unit for this directory (idempotent) |
| `status` | units, candidates, leases, waves (`simulate`, `health`, `gc`, `short`) |
| `declare` | declare scopes + acquire leases; `dry_run`, `renew`, `release`, `decide` |
| `commit` | commit the worktree and register the candidate (`sync` rebases first) |
| `handoff` | **agent's last step**: verify + trial-merge into an uncommitted draft on main |
| `integrate` | campaign landing: merge a verified candidate onto the feature branch (`node`, `check_only`, `acceptance`, `gpu`, `cleanup`) |
| `report` | write the deterministic campaign report skeleton, plus `narrative` |

`handoff` is the only landing-adjacent action in the tool. It stages an
**uncommitted draft** on main and returns it (files, diffstat, commit subject,
`open_command`). Approval is a human action and is not exposed to the model.

### Approving from inside the session

You do **not** need a terminal. The human owns the handoff boundary, gated by a
blocking confirmation dialog:

- **Run a command.** `/ig-approve` shows the staged draft and, on confirmation,
  commits it on main and cleans up the unit worktrees. `/ig-reject` discards the
  draft and restores main. With no pending handoff, `/ig-approve` tells you to
  ask the agent to run `ig handoff`.
- **Ask the agent** to run `ig handoff`, then run `/ig-approve` yourself. The
  agent cannot commit or approve; only the human confirmation does.

Approval still requires `ctx.hasUI` (TUI or RPC mode). In print/JSON mode the
extension refuses and points at the equivalent CLI commands.

## Campaign orchestration (the `campaign` tool)

For delivering a design document as a set of components on a feature branch, the
`campaign` extension drives a top-level coordinator that spawns planner/worker/
verifier subagents while `intergent` stays the deterministic engine. The DAG in
`.intergent/<branch-key>.dag.json` is the only schedule. See
[`docs/orchestration.md`](../../docs/orchestration.md).

```bash
cp integrations/pi/campaign.ts ~/.pi/agent/extensions/campaign.ts
mkdir -p ~/.pi/agent/campaign-agents
cp integrations/pi/agents/*.md ~/.pi/agent/campaign-agents/
INTERGENT_AUTO_BOOTSTRAP=0 pi    # launch the coordinator on the base checkout
```

The `campaign` tool has actions `start`, `status`, `ready`, `spawn`, `verify`,
`integrate`, and `report`. Workers are child processes of the coordinator and
are not detached, so a crash kills them and a resumed `start` resets `running`
nodes. Only the verifier is given `tools/gpu.sh`; workers never touch the GPU.

## Alternative: the skill (CLI only, no extension)

The repository root is a self-contained Agent Skill with the CLI bundled. Install
it globally (pi is auto-detected):

```bash
npx skills add ming6ao/intergent -g -y -a pi
```

Pi then discovers `intergent` and, when a task matches, follows the CLI workflow
(https://agentskills.io). You can also force it with `/skill:intergent`.

The same skill works unchanged in Claude Code, Codex, Cursor, and other harnesses
that implement the Agent Skills standard.
