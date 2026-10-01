---
name: intergent
description: 'Deliver a design document as landed work on a feature branch using the Intergent campaign orchestrator: a coordinator turns the design into a DAG of planner/worker/verifier subagents, each worker isolated by a git worktree and scope leases, each candidate verified against a fingerprint before integration. Use ONLY when the user explicitly invokes this skill: runs `/skill:intergent`, or names it ("intergent"/"ig") and asks to run a campaign, coordinate parallel agents, or land a feature branch. Do NOT auto-load it merely because a repository contains .intergent/config.json. Bundles the intergent CLI. Not for read-only research.'
license: Apache-2.0
disable-model-invocation: true
metadata:
  version: "0.2.0"
  author: intergent
---

# Intergent

Intergent delivers a design document as a set of components on a **feature
branch**. A top-level **coordinator** turns the design into a machine-readable
DAG (`dag.json`) and drives planner, worker, and verifier subagents. The
`intergent` engine owns isolation, leases, verification, and integration.

```text
COORDINATOR (this session)
 ├── PLANNER   reads the design, writes dag.json
 ├── WORKER_*  one-shot per ready DAG node: its own worktree + lease
 │               declare -> edit -> acceptance (CPU) -> commit
 └── VERIFIER  read-only per candidate: T0 CPU then tools/gpu.sh
```

- The DAG is the **only schedule** — there is no phase/wave engine in the
  scheduler. `ready(n) := every d in n.depends_on is done`, where `done` means
  verified **and integrated** onto the feature branch.
- No two workers write the same file: each declares scope leases before editing.
- Only the verifier may use the GPU. Workers never touch it.
- Everything is reconstructable from `.intergent/` + git after a crash.

## Locate the bundled CLI

The CLI lives at `bin/intergent` **relative to the directory containing this
SKILL.md** (the pi package also resolves it automatically). Resolve it before
running:

```bash
# $SKILL_DIR is the folder containing this SKILL.md
IG="$SKILL_DIR/bin/intergent"   # absolute path to the bundled CLI
python3 "$IG" --version
```

Always invoke it as `python3 "$IG" ...`. Do **not** wrap the command string in
quotes. If `intergent` is already on `PATH`, use it directly. Add `--json` for
parseable output.

## Hard rules

1. **Workers run inside their unit worktree.** `intergent status --short` must
   succeed; otherwise stop. Never edit the main working tree.
2. **Declare before editing.** No edits before `intergent declare` returns
   `granted`.
3. **A worker's last step is `commit`.** Workers never run `integrate` or
   `git merge`. The coordinator owns verification and integration.
4. **Never force a conflict.** If `declare` returns `queued`, exit and report the
   blocker; if it returns `needs_decision`, stop — the coordinator re-plans the
   node.
5. **The GPU is the verifier's.** T0 CPU is the inner loop; GPU acceptance goes
   through `tools/gpu.sh --tier <T1|T2> -- <command>`.

## Campaign loop (the coordinator)

Use the `campaign` tool in pi, or the CLI directly:

```bash
# 1. Create/verify the feature branch and run the planner -> dag.json
intergent start --no-unit --main feat/example --base main
#    (in pi: campaign start with design/feature_branch/base)

# 2. ready -> spawn each node whose dependencies are done
intergent start --name w1 --base feat/example --kind worker
#    worker declares, edits, runs acceptance, commits

# 3. verify the latest prepared candidate (records a node verdict)
intergent integrate --node w1 --check-only --acceptance "<cmd>" --gpu none

# 4. land the verified candidate immediately, before spawning dependents
intergent integrate --node w1

# 5. final idempotent sweep, then report
intergent integrate
intergent report --narrative "what changed / risks"
```

The coordinator's own checkout is **not** an Intergent unit; bootstrap the plane
with `--no-unit`. Promotion from the feature branch to the default branch is a
human `git` step — `integrate` refuses the plane's recorded default branch.

## Unit actions (used by workers and the coordinator)

| Action | Purpose |
|---|---|
| `start` | bootstrap the plane; `--no-unit` for the coordinator's checkout; `--name N --base <feature>` creates a worker unit |
| `status` | units, candidates, leases, waves; `--short`, `--unit U`, `--simulate`, `--health` |
| `declare` | declare scopes and acquire leases; `--dry-run`, `--renew`, `--release` |
| `commit` | commit the worktree and register the candidate |
| `integrate` | merge a verified candidate onto the feature branch; `--node`, `--acceptance`, `--gpu`, `--check-only`, `--cleanup` |
| `report` | write the deterministic campaign report; `--narrative` appends the coordinator's summary |

## Worker workflow

```bash
intergent --json declare --operation modify \
  --scope "symbol:src/login.py#Login.run" --scope "file:docs/auth.md"
# ... edit only your worktree ...
intergent commit -m "add scope check to Login" --summary "scope check"
```

`declare` responses:

| status | Meaning | What to do |
|---|---|---|
| `granted` | Leases acquired | Proceed with edits |
| `queued` | Another unit holds an overlapping scope | Exit and report the blocker; the coordinator serializes or re-plans |
| `needs_decision` | Destructive vs additive on an exact scope | Stop; the coordinator re-plans the node |

Scope syntax is `kind:key[=operation]`, e.g. `file:src/api/routes.py`,
`symbol:src/api/routes.py#UserRouter.create`, `dir:src/api`, `config:deploy.timeout`,
`schema:users.email`, `migration:0007_add_scope`, `api:GET /users/{id}`.
Operations `add`/`extend`/`modify` are additive; `replace`/`remove`/`rename`/
`migrate` are destructive and queue behind a holder.

Full specification: [docs/orchestration.md](./docs/orchestration.md). CLI/MCP
reference: [docs/implementation.md](./docs/implementation.md).
