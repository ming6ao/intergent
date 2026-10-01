---
name: intergent
description: 'Deliver a design document as landed work on a feature branch using the Intergent campaign orchestrator: a coordinator turns the design into a DAG of planner/worker/verifier subagents, each worker isolated by a git worktree, each node owning disjoint directories (plan-time ownership), each candidate verified against a fingerprint before integration. Use ONLY when the user explicitly invokes this skill: runs `/skill:intergent`, or names it ("intergent"/"ig") and asks to run a campaign, coordinate parallel agents, or land a feature branch. Do NOT auto-load it merely because a repository contains .intergent/config.json. Requires the intergent pi package. Not for read-only research.'
license: Apache-2.0
disable-model-invocation: true
metadata:
  version: "0.3.0"
  author: intergent
---

# Intergent

Intergent delivers a design document as a set of components on a **feature
branch**. A top-level **coordinator** turns the design into a machine-readable
DAG (`dag.json`) and drives planner, worker, and verifier subagents. The
`intergent` engine owns isolation, verification, and integration; all
serialization is decided at **plan time** from directory ownership.

```text
COORDINATOR (this session)
 ├── PLANNER   reads the design, writes dag.json
 ├── WORKER_*  one-shot per ready DAG node: its own worktree
 │               edit owned dirs -> acceptance (CPU) -> commit
 └── VERIFIER  read-only per candidate: T0 CPU then tools/gpu.sh
```

- The DAG is the **only authored schedule**. Waves are a deterministic
  projection of it: nodes are packed into waves by `owns` **directory-subtree
  overlap** and `depends_on`, capped by `concurrency` (default 3).
  `ready(n) := every d in n.depends_on is done` **and** `n` is in the current
  wave, where `done` means verified **and integrated** onto the feature branch.
- A node owns **directories, not files**. For each path it will add, modify, or
  delete it declares the deepest directory that contains it (`dir:src/api`).
  Two nodes whose owned directories overlap (equal, ancestor, or descendant)
  are serialized into different waves. There is no runtime declare/lease step.
- A node starts only in the current wave; a later wave begins after every
  member of the previous wave is integrated, so its units fork from the updated
  feature branch.
- Only the verifier may use the GPU. Workers never touch it.
- Cleanup is grouped by wave: the coordinator asks once per completed wave.
- `commit` enforces **plan conformance**: a worker whose commit changes a path
  outside its node's owned directories is rejected, and the coordinator widens
  `owns` or adds a `depends_on` edge (the DAG fingerprint changes, so the next
  `status`/`ready`/`spawn` replans).
- Everything is reconstructable from `.intergent/` + git after a crash.

The pi package provides two tools: `campaign` for the coordinator and `ig` for
workers. Both register **inactive**, so a plain session never lists them or
their prompt guidelines; `/skill:intergent <design.md>` activates them for the
session when the argument is an existing design document. `runSubagent` applies
each subagent's `tools:` allowlist, so a worker gets `ig` but never `campaign`,
and the verifier gets neither.

## Hard rules

1. **Workers run inside their unit worktree.** `ig` `action: status`
   (`short: true`) must succeed; otherwise stop. Never edit the main working
   tree.
2. **Edit only your node's owned directories.** Ownership is declared in
   `dag.json` and enforced by `commit`; a rejected commit means the planner
   under-declared, not that you should widen your own scope.
3. **A worker's last step is `commit`.** Workers never call `ig` `integrate` or
   `git merge`. The coordinator owns verification and integration.
4. **Ordering is authored in the DAG.** If two nodes would touch the same
   directory, the planner must put them in different waves (disjoint `owns`) or
   add a `depends_on` edge. Never rely on runtime arbitration.
5. **The GPU is the verifier's.** T0 CPU is the inner loop; GPU acceptance goes
   through `tools/gpu.sh --tier <T1|T2> -- <command>`.

## Campaign loop (the coordinator)

Use the `campaign` tool:

```
campaign start <DESIGN.md>   adopt current branch + planner -> dag.json + waves
campaign ready               current-wave nodes whose dependencies are integrated
campaign status              waves + DAG + live child state
campaign spawn <node>        one-shot worker in its own worktree
campaign verify <node>       read-only verifier; records a verdict
campaign integrate <node>    land the verified candidate, before spawning dependents
campaign report              deterministic report (`--narrative` appends the summary)
```

`start` projects the DAG into waves (directory-subtree overlap, `depends_on`
barrier, `concurrency` cap; default 3) and stores them in `state.json`. A node
may only spawn in the current wave; when every member of a wave is integrated
the next wave opens and its units fork from the updated feature branch. A
coordinator-added `depends_on` edge changes the DAG fingerprint and the next
`status`/`ready`/`spawn` automatically replans the waves.

The coordinator's own checkout is **not** an Intergent unit; `campaign start`
bootstraps the plane with `--no-unit` and adopts the **currently checked-out
branch** as the campaign feature branch — it never creates one. Starting on the
repository default branch is refused; promotion from the feature branch to the
default branch is a human `git` step (`integrate` refuses the plane's recorded
default branch).

## Unit actions (the `ig` tool)

| Action | Purpose |
|---|---|
| `start` | bootstrap the plane; `no_unit: true` for the coordinator's checkout; `name` + `base` creates a worker unit |
| `status` | units, candidates, waves; `short`, `unit`, `simulate`, `health`, `gc` |
| `commit` | commit the worktree, enforce plan conformance, and register the candidate |
| `integrate` | merge a verified candidate onto the feature branch; `node`, `acceptance`, `gpu`, `check_only`, `cleanup` |
| `report` | write the deterministic campaign report; `narrative` appends the coordinator's summary |

## Worker workflow

A spawned worker calls the `ig` tool from its own worktree:

```
ig action: status, short: true
# ... edit only files under your node's owned directories ...
ig action: commit, message: "add scope check to Login", summary: "scope check"
```

Ownership syntax is `dir:PATH` (a bare path is also accepted), always a
directory at the deepest level that contains the paths the node touches:
`dir:src/api`, `dir:src`, or `dir:.` for repository-root files. A non-directory
spec (`file:`, `symbol:`, ...) is rejected when the DAG is projected, so a
campaign cannot start with file-level ownership.

If `commit` rejects a path outside the owned directories, report it and stop.
The coordinator widens the node's `owns` (or adds a `depends_on` edge) in
`dag.json`; the next `status`/`ready`/`spawn` replans the waves.

Full specification: [docs/guide.md](./docs/guide.md). Tool
reference: [docs/reference.md](./docs/reference.md).
