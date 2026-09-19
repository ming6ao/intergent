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
| `review` | review packet for the human |
| `submit` | **human-gated**: show the packet, ask for confirmation, then approve + land |

`submit` is the only landing path in the tool, and it always asks the human via
an interactive confirmation; without a UI it refuses. `review --approve`,
`--reject`, and `--land` are not exposed to the model.

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
