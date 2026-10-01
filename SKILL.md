---
name: intergent
description: 'Deliver a design document as landed work on a feature branch using the Intergent campaign orchestrator: a coordinator turns the design into a DAG of planner/worker/verifier subagents, each worker isolated by a git worktree and scope leases, each candidate verified against a fingerprint before integration. Use ONLY when the user explicitly invokes this skill: runs `/skill:intergent`, or names it ("intergent"/"ig") and asks to run a campaign, coordinate parallel agents, or land a feature branch. Do NOT auto-load it merely because a repository contains .intergent/config.json. Requires the intergent pi package. Not for read-only research.'
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

The pi package provides two tools: `campaign` for the coordinator and `ig` for
workers. `runSubagent` applies each subagent's `tools:` allowlist, so a worker
gets `ig` but never `campaign`, and the verifier gets neither.

## Hard rules

1. **Workers run inside their unit worktree.** `ig` `action: status`
   (`short: true`) must succeed; otherwise stop. Never edit the main working
   tree.
2. **Declare before editing.** No edits before `ig` `action: declare` returns
   `granted`.
3. **A worker's last step is `commit`.** Workers never call `ig` `integrate` or
   `git merge`. The coordinator owns verification and integration.
4. **Never force a conflict.** If `declare` returns `queued`, exit and report the
   blocker; if it returns `needs_decision`, stop — the coordinator re-plans the
   node.
5. **The GPU is the verifier's.** T0 CPU is the inner loop; GPU acceptance goes
   through `tools/gpu.sh --tier <T1|T2> -- <command>`.

## Campaign loop (the coordinator)

Use the `campaign` tool:

```
campaign start <DESIGN.md>   feature branch + planner -> dag.json
campaign ready               nodes whose dependencies are integrated
campaign status              DAG + live child state
campaign spawn <node>        one-shot worker in its own worktree
campaign verify <node>       read-only verifier; records a verdict
campaign integrate <node>    land the verified candidate, before spawning dependents
campaign report              deterministic report (`--narrative` appends the summary)
```

The coordinator's own checkout is **not** an Intergent unit; `campaign start`
bootstraps the plane with `--no-unit`. Promotion from the feature branch to the
default branch is a human `git` step — `integrate` refuses the plane's recorded
default branch.

## Unit actions (the `ig` tool)

| Action | Purpose |
|---|---|
| `start` | bootstrap the plane; `no_unit: true` for the coordinator's checkout; `name` + `base` creates a worker unit |
| `status` | units, candidates, leases, waves; `short`, `unit`, `simulate`, `health` |
| `declare` | declare scopes and acquire leases; `dry_run`, `renew`, `release` |
| `commit` | commit the worktree and register the candidate |
| `integrate` | merge a verified candidate onto the feature branch; `node`, `acceptance`, `gpu`, `check_only`, `cleanup` |
| `report` | write the deterministic campaign report; `narrative` appends the coordinator's summary |

## Worker workflow

A spawned worker calls the `ig` tool from its own worktree:

```
ig action: declare, operation: modify,
   scope: ["symbol:src/login.py#Login.run", "file:docs/auth.md"]
# ... edit only your worktree ...
ig action: commit, message: "add scope check to Login", summary: "scope check"
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

Full specification: [docs/orchestration.md](./docs/orchestration.md). Tool
reference: [docs/implementation.md](./docs/implementation.md).
