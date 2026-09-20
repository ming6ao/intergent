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
| `verify` | fingerprint-pinned trusted checks |
| `review` | review packet for the human (includes an `open_command` for the worktree) |
| `submit` | **human-gated**: stage the wave as an uncommitted draft on main, show it, and commit only on confirmation |

`submit` is the only landing path in the tool. It stages an **uncommitted draft**
on main, shows the human the drafted files/stat/commit message, and only creates
the commit after the in-session confirmation dialog; without a UI it refuses.
`review --approve`, `--reject`, and `--land` are not exposed to the model.

### Approving from inside the session

You do **not** need a terminal. There are two human paths, both gated by a
blocking confirmation dialog:

- **Ask the agent.** Say e.g. "approve and land it". The agent calls
  `ig action=submit` (with the candidate id when there is more than one). That
  approves the candidate and stages the combined change as an **uncommitted
  draft** on main, then shows you the draft (files, diffstat, commit subject, and
  the `open_command` to inspect main). Confirming creates the commit; declining
  runs `--abort` and restores main. The agent cannot fake either step.
- **Run a command.** `/ig-approve [candidate]` does the same draft → confirm →
  commit; `/ig-reject [candidate]` rejects; `/ig-land` lands every approved
  candidate. With no argument, the matching candidate is resolved from `status`,
  preferring this session's worktree; if several match, you get a picker.

Approval still requires `ctx.hasUI` (TUI or RPC mode). In print/JSON mode the
extension refuses and points at the equivalent CLI commands.

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
