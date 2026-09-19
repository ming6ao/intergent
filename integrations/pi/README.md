# Intergent + pi

pi has **no built-in MCP** by design. It recommends either CLI tools documented
by a skill, or an extension that adds the tools. This directory provides both.

## Recommended: the extension (native tools)

The extension `intergent.ts` registers `ig_*` tools that call the `intergent`
CLI, resolving the current unit from pi's working directory.

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

3. Start pi **inside the Intergent unit worktree** and reload:

   ```bash
   intergent workspace create auth-fix --agent pi
   cd "$(intergent --json workspace current | python3 -c 'import sys,json;print(json.load(sys.stdin)["worktree"])')"
   pi            # then /reload if pi was already running
   ```

If the binary is not on `PATH`, set `INTERGENT_BIN` in the environment before
launching pi, e.g. `INTERGENT_BIN=/home/me/intergent/bin/intergent pi`.

### Tools

| Tool | Purpose |
|---|---|
| `ig_current` | show the unit for this worktree |
| `ig_declare` | declare scopes + operation, acquire leases |
| `ig_check` | dry-run conflict check |
| `ig_heartbeat` / `ig_release` | renew / release leases |
| `ig_commit` / `ig_finish` | commit, register candidate |
| `ig_verify` | fingerprint-pinned trusted checks |
| `ig_review` | review packet for the human |
| `ig_status` / `ig_simulate` | status and combined-tree wave plan |

`approve` and `land` are intentionally not exposed; they are human actions.

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
