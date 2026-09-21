---
name: intergent
description: Coordinate this session with other coding agents on one repository using the Intergent local plane (git worktree per session, declared scope leases, fingerprint-pinned verification, approval-gated landing). Use ONLY when the user explicitly invokes this skill: runs `/skill:intergent`, or names it ("intergent"/"ig") and asks to coordinate parallel agents, declare scopes, or land a wave. Do NOT auto-load it merely because a repository contains .intergent/config.json. Bundles the intergent CLI. Not for read-only research.
license: Apache-2.0
disable-model-invocation: true
metadata:
  version: "0.2.0"
  author: intergent
---

# Intergent

Intergent lets several coding-agent sessions work the same repository in
parallel without authoring conflicting changes. This session owns one **unit**
(git worktree + branch). Changes land on the local main branch only after a
human approves them.

This skill bundles the Intergent CLI. Requires `git` and Python 3.11+.

## The action surface

There are **seven** actions, shared by the CLI, MCP, and the pi tool. Most are
overloaded by flags, so the surface stays small:

| Action | Purpose |
|---|---|
| `start` | bootstrap the plane + a unit for this directory (idempotent; alias `init`) |
| `declare` | declare scopes and acquire leases; `--dry-run` checks, `--renew`/`--release` manage leases, `--decide` resolves a conflict |
| `commit` | commit the worktree and register the candidate; `--sync` rebases first |
| `verify` | run trusted checks at the candidate's exact commit |
| `review` | show the review packet; humans may add `--approve` / `--reject` / `--land` |
| `submit` | (human) approve + land a candidate in one step |
| `status` | units, candidates, leases, waves; `--health`, `--simulate`, `--gc`, `--short`, `--unit U` |

Agents can call `start`, `declare`, `commit`, `verify`, `review`, `status`.
`submit` and the `review` approval flags are human-only.

## Locate the bundled CLI

The CLI lives at `bin/intergent` **relative to the directory containing this
SKILL.md**. Resolve it to an absolute path before running, e.g.:

```bash
# $SKILL_DIR is the folder containing this SKILL.md
IG="$SKILL_DIR/bin/intergent"   # absolute path to the bundled CLI
python3 "$IG" --version
```

Always invoke it as `python3 "$IG" ...` (or an array). Do **not** wrap a
command string in quotes — `IG="python3 $SKILL_DIR/bin/intergent"; "$IG" ...`
is treated as one executable name and fails with `No such file or directory`.

If `intergent` is already installed on `PATH`, you may use it directly instead:

```bash
command -v intergent && intergent --version
```

Add `--json` for parseable output.

## Hard rules

1. **Work inside your unit worktree.** Run `intergent status --short`. If it
   fails, you are not in a unit worktree — stop and tell the human to launch
   you inside one (see Setup). Do not edit the main working tree.
2. **Declare before you edit.** No file edits before `intergent declare`
   returns `granted`. For read-only work, skip Intergent.
3. **Never approve or land.** `submit` and the `review` approval flags are human
   actions. Exception: if your harness exposes a human-gated approval tool that
   requires a fresh in-session confirmation (e.g. the pi `ig` `submit` action),
   call it only when the human explicitly asks. Never claim a merge happened
   unless the tool reports success.
4. **Never override a conflict** unless the human explicitly asks. If a
   declaration returns `needs_decision`, stop and surface the options.
5. **Heartbeat** during long tasks (`intergent declare --renew`) so your lease
   does not expire.

## Setup (only when not already in a unit worktree)

A human (or you, then ask them to relaunch) runs:

```bash
WT=$(intergent --json start --agent <agent-name> | python3 -c 'import sys,json;print(json.load(sys.stdin)["worktree"])')
cd "$WT"
```

Then launch the agent **inside that directory**, so its cwd is the unit
worktree. `start` is idempotent and is the only bootstrap command.

## Per-task workflow

```bash
# 1. Declare the exact scopes you will touch. Prefer narrow symbol/file scopes.
intergent --json declare --operation modify \
  --scope "symbol:src/login.py#Login.run" \
  --scope "file:docs/auth.md"

# 2. Edit files in this worktree only.

# 3. Commit and register the candidate in one call.
intergent commit -m "add scope check to Login" --summary "scope check"

# 4. Verify the exact commit (trusted checks + fingerprint).
intergent --json verify <candidate-id>

# 5. Show the review packet and report the candidate to the human, including
#    the packet's `open_command` so they can open the worktree (VS Code).
intergent --json review <candidate-id>
```

The human approves and lands from their client (or a terminal):

```bash
intergent review <candidate-id> --approve
intergent review <candidate-id> --land            # removes the unit worktree
# or, in one step:
intergent submit <candidate-id>
```

Landing removes the unit worktree by default (the branch is kept, so the
history stays reachable). Pass `--keep` to inspect it afterwards.

`declare` responses:

| status | Meaning | What to do |
|---|---|---|
| `granted` | Leases acquired | Proceed with edits |
| `queued` | Another unit holds an overlapping scope (`position`, `blocker`, `eta_seconds`) | Do non-conflicting work, or `declare --renew` and wait; after the blocker releases, `commit --sync` and retry |
| `needs_decision` | Destructive vs additive on an exact scope | **Stop** and ask the human to choose `wait`, `redesign`, or `override` |

## Scope syntax

`kind:key[=operation]`, for example:

- `file:src/api/routes.py`
- `symbol:src/api/routes.py#UserRouter.create`
- `dir:src/api`
- `config:deploy.timeout`
- `schema:users.email`
- `migration:0007_add_scope`
- `api:GET /users/{id}`

Operations: `add`, `extend`, `modify` (additive) and `replace`, `remove`,
`rename`, `migrate` (destructive). Destructive declarations of a scope another
unit holds are queued or escalated — expect a wait.

## Useful commands

```bash
intergent --json status                 # units, candidates, lease queue, waves
intergent --json declare --dry-run --operation modify --scope file:x.py
intergent --json declare --renew        # renew leases
intergent --json declare --release      # release early (e.g. abandoning the task)
intergent --json status --simulate      # plan waves over the combined tree
intergent --json status --health        # git/plane health check
intergent --json review <candidate>     # review packet for the human
```

## Human approval and landing

Agents never land. The human runs:

```bash
intergent status
intergent review <candidate>          # also prints `open_command` for the worktree
intergent review <candidate> --approve
intergent review --land --all        # stage the wave as an uncommitted draft on main
# inspect main (use the draft's `open_command`), then either:
intergent review --land --commit     # commit the draft (removes unit worktrees)
intergent review --land --abort      # discard the draft and restore main
```

Reference docs ship alongside this skill: `docs/agents.md`,
`docs/implementation.md`, `docs/local-plane.md`.
