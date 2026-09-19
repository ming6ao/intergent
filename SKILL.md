---
name: intergent
description: Coordinate this session with other coding agents on one repository using the Intergent local plane (git worktree per session, declared scope leases, fingerprint-pinned verification, approval-gated landing). Use when the repository has .intergent/config.json, when the user mentions Intergent/ig/scope conflicts/wave landing, or before editing files in a coordinated multi-agent repo. Bundles the intergent CLI. Not for read-only research.
license: Apache-2.0
metadata:
  version: "0.1.0"
  author: intergent
---

# Intergent

Intergent lets several coding-agent sessions work the same repository in
parallel without authoring conflicting changes. This session owns one **unit**
(git worktree + branch). Changes land on the local main branch only after a
human approves them.

This skill bundles the Intergent CLI. Requires `git` and Python 3.11+.

## Locate the bundled CLI

The CLI lives at `bin/intergent` **relative to the directory containing this
SKILL.md**. Resolve it to an absolute path before running, e.g.:

```bash
# $SKILL_DIR is the folder containing this SKILL.md
IG="python3 $SKILL_DIR/bin/intergent"
"$IG" --version
```

If `intergent` is already installed on `PATH`, you may use it directly instead:

```bash
command -v intergent && intergent --version
```

Every command below uses `intergent`; substitute `python3 $SKILL_DIR/bin/intergent`
when it is not on `PATH`. Add `--json` for parseable output.

## Hard rules

1. **Work inside your unit worktree.** Run `intergent workspace current`. If it
   fails, you are not in a unit worktree — stop and tell the human to launch
   you inside one (see Setup). Do not edit the main working tree.
2. **Declare before you edit.** No file edits before `intergent declare`
   returns `granted`. For read-only work, skip Intergent.
3. **Never approve or land.** `approve`, `reject`, and `land` are human actions.
4. **Never override a conflict** unless the human explicitly asks. If a
   declaration returns `needs_decision`, stop and surface the options.
5. **Heartbeat** during long tasks so your lease does not expire.

## Setup (only when not already in a unit worktree)

A human (or you, then ask them to relaunch) runs:

```bash
intergent workspace create <short-task-name> --agent <agent-name>
cd "$(intergent --json workspace current | python3 -c 'import sys,json;print(json.load(sys.stdin)["worktree"])')"
```

Then launch the agent **inside that directory**, so its cwd is the unit worktree.

## Per-task workflow

```bash
# 1. Declare the exact scopes you will touch. Prefer narrow symbol/file scopes.
intergent --json declare --operation modify \
  --scope "symbol:src/login.py#Login.run" \
  --scope "file:docs/auth.md"

# 2. Edit files in this worktree only.

# 3. Commit, then register the candidate.
intergent commit -m "add scope check to Login"
intergent --json finish

# 4. Verify the exact commit (trusted checks + fingerprint).
intergent --json verify <candidate-id>

# 5. Report the candidate and review summary to the human. They approve and land.
intergent --json review <candidate-id>
```

`declare` responses:

| status | Meaning | What to do |
|---|---|---|
| `granted` | Leases acquired | Proceed with edits |
| `queued` | Another unit holds an overlapping scope (`position`, `blocker`, `eta_seconds`) | Do non-conflicting work, or `heartbeat` and wait; after the blocker releases, `rebase` and retry |
| `needs_decision` | Destructive vs additive on an exact scope | **Stop** and ask the human to choose `wait`, `redesign`, or `override` |
| error | Not in a unit worktree, or invalid scope | Fix per Hard rules |

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
intergent --json check --operation ...  # dry-run conflict check, takes no lease
intergent --json heartbeat              # renew leases
intergent --json release                # release early (e.g. abandoning the task)
intergent --json simulate               # plan waves over the combined tree
intergent --json review <candidate>     # review packet for the human
```

## Human approval and landing

Agents never land. The human runs:

```bash
intergent status
intergent review <candidate>
intergent approve <candidate>
intergent land --all          # transactional merge into local main
```

Reference docs ship alongside this skill: `docs/agents.md`,
`docs/implementation.md`, `docs/local-plane.md`.
