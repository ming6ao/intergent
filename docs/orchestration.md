# Orchestration: delivering a design with coordinated subagents

> Status: **implemented** (S0 core and S1 orchestrator/agents; S2 resilience is
> implemented at the coordinator level). The `intergent` core additions in §6
> are additive to the existing local-plane reference; §6.4 extends the
> verification store (source tagging and node acceptance fingerprints). This
> document supersedes the deferral of process supervision in
> [Agent integration](./agents.md) ("`agent start`-style process
> supervision … not implemented yet").

This document specifies how one **top-level coordinator session** turns a design
document into landed work by spawning a planner, workers, and a verifier, while
`intergent` remains the deterministic state, isolation, and integration engine.

## 1. Goals and non-goals

**Goals**

- Deliver a design document as a set of components on a **feature branch**.
- One **coordinator** (the top-level session) owns the plan, spawns agents, and
  reports; the user talks to the coordinator directly.
- Workers are isolated by `intergent` units (one worktree + branch each) and by
  scope leases; no two workers write the same file.
- A **single machine-readable artifact**, `dag.json`, is the plan. No prose plan.
- Verification is independent of authoring and is the only GPU consumer.
- State survives a crash: everything is reconstructable from `.intergent` + git.

**Non-goals**

- Steering a worker mid-task (workers are one-shot; messages go through the
  coordinator). RPC-managed, steerable workers are a later, separate upgrade.
- Multiple concurrent campaigns per plane. One plane integrates one feature
  branch.
- A second scheduler. The DAG is the only schedule; there is no separate
  "wave" or "phase" engine.

## 2. Topology

```text
USER
 │  launches pi in the repository, on the feature-branch checkout
 ▼
COORDINATOR  (top-level pi session — the session the user sees)
 │  tools: campaign start | status | ready | spawn | verify | integrate | report
 │  UI:    widget with id / role / node / busy-idle / log
 ├── PLANNER   (subagent, invoked by the coordinator via `start`)
 │               reads the design, writes dag.json (plane state, not committed)
 ├── WORKER_*  (one-shot subagent per ready DAG node, own intergent worktree)
 │               declare -> edit -> acceptance tests -> commit  (never GPU)
 └── VERIFIER  (read-only subagent, invoked per candidate)
                 T0 CPU then tools/gpu.sh; returns a verdict; never edits
```

Rules that fall out of this shape:

- The coordinator is **not** an `intergent` unit and must not be bound to a unit
  worktree. The campaign extension drives the engine for it. Plane bootstrap
  uses the engine's `start --no-unit`, so campaign setup does not create a
  phantom unit in the coordinator's checkout.
- The planner is invoked by the coordinator, not by the user, and may be
  re-invoked (`start --replan`) after a failure.
- Workers and the verifier are subagents. Workers run inside their unit
  worktree and get normal `intergent` behaviour; the verifier has no lease.

## 3. The plan artifact: `dag.json`

`dag.json` is canonical. There is no `plan.md`. A human-readable summary is
printed by `campaign status` / `campaign start`; it is never written to disk.

```jsonc
{
  "campaign": "nanochat-cpp",
  "feature_branch": "feat/nanochat-cpp",
  "base": "master",
  "design": "DESIGN.md",
  "concurrency": 4,
  "nodes": [
    { "id": "w1", "label": "runtime-core", "phase": "P0",
      "goal": "Tensor and the CPU reference backend for all of kernels.h",
      "owns": ["dir:backends/cpu", "file:src/tensor.cc"],
      "depends_on": [],
      "acceptance": ["bazel test //... --test_tag_filters=-gpu"],
      "gpu": "none" },

    { "id": "w2", "label": "oracle", "phase": "P0",
      "owns": ["dir:tests", "file:tools/dump_oracle.py"],
      "depends_on": ["w1"],
      "acceptance": ["bazel test //tests:oracle_test"],
      "gpu": "none" },

    { "id": "w3", "label": "kernel-rms", "phase": "P1",
      "owns": ["file:backends/cuda/kernels/rms_norm.cu",
               "file:dev/kernels/rms_norm.cu"],
      "depends_on": ["w1"],
      "acceptance": ["//dev/kernels:rms_norm_fd_test"],
      "gpu": "T1" }
  ]
}
```

Fields:

| Field | Meaning |
|---|---|
| `id` | Worker id and unit name; also the log suffix (`worker_<id>.log`). |
| `label` | Human label for the report and dashboard. |
| `phase` | **Display/report grouping only.** Never used for scheduling. |
| `goal` | The prompt seed handed to the worker. |
| `owns` | Scope specs the worker must `declare` before editing. |
| `depends_on` | Node ids that must be `done` before this node is `ready`. |
| `acceptance` | Commands the worker must run and the verifier re-runs. |
| `gpu` | `none`, `T1`, or `T2`; only the verifier may use it. |

**There are no phases in the scheduler.** A graph plus the rule

```text
ready(n)  :=  every d in n.depends_on is done
```

is the whole executor. `done` means **verified *and* integrated** onto the
feature branch (§5), so a dependent's `--base <feature_branch>` checkout always
contains its dependencies' code. Batching integration until the end would make
`depends_on` meaningless. A barrier is expressed by depending on every node of
the prior group, or by adding an explicit aggregation node. `phase` survives
only as a label so reports can group components.

## 4. State layout

All campaign state lives under `.intergent/`, **prefixed by the feature-branch
name** so one campaign's files form a single glob and no two campaigns collide.

Let `branch-key` replace `/` with `--`:

```text
feat/nanochat-cpp   ->   feat--nanochat-cpp
```

```text
.intergent/
  config.json                          # plane config (as today)
  state.db                             # units, candidates, leases, verifications
  feat--nanochat-cpp.dag.json          # canonical plan
  feat--nanochat-cpp.state.json        # executor progress (node -> status)
  feat--nanochat-cpp.report.md         # final report (kept on cleanup)
  feat--nanochat-cpp.worker_w1.log     # one log per worker id
  feat--nanochat-cpp.worker_w3.log
```

`state.json` holds only what git and `state.db` cannot express quickly: per-node
`pending|running|done|failed`, the last verdict, and attempt counts.

`dag.json` is **plane state, not repository content**: it lives under the
git-excluded `.intergent/` directory and is never committed. There is no "plan
unit"; the planner writes it through the coordinator, not through a worker
worktree.

Crash recovery: `campaign start` with an existing `dag.json` loads it, rebuilds
node status from `intergent status` (candidates `prepared`/`landed`, unit states) and
`state.json`, and resumes. **Precedence on conflict: git and `state.db` are
authoritative; `state.json` is a rebuildable cache.** A node that `state.json`
calls `running` but whose unit has no live process and no `prepared` candidate
is reset to `pending` (git may show a dirty worktree; the worktree is reset
before re-spawn). In-memory state is never trusted.

## 5. Lifecycle

```text
campaign start <design>            # adopt current branch, invoke planner, write dag.json
        │
        ▼  (user may review the printed summary)
ready ──► spawn (<= concurrency) ──► worker commits candidate
  ▲                                        │
  │                                        ▼
  │                          verify (verifier: T0, then gpu.sh)
  │                                        │ pass
  │                                        ▼
  └──── node done ◄── integrate --node <id> (lands dep before dependents)
                  │  failed -> retry / split / stop (coordinator decides)
                  ▼
        all nodes done or stopped
                  │
                  ▼
integrate (sweep) ──► offer cleanup (worktrees / artifacts) ──► report
```

1. **`start`** — adopts the **currently checked-out branch** as the campaign
   feature branch (it never creates one; starting on the repository default
   branch is refused), bootstraps the plane with `intergent start --no-unit`
   whose integration branch is that branch, and **invokes the planner** in the
   same action. The planner reads the design and writes `dag.json`; `start`
   prints the branch it adopted plus the summary, and stops for an optional
   human look.
2. **`ready`** — returns the nodes whose dependencies are all `done`.
3. **`spawn`** — creates an `intergent` unit for a node
   (`intergent start --name <id> --base <feature_branch>`), launches a one-shot worker
   in that worktree, and tees output to `worker_<id>.log`. Spawning a node is
   only allowed once **every dependency has been integrated** onto the feature
   branch (not merely verified), so the node's base checkout already contains
   its dependencies' code.
4. **`verify`** — spawns the read-only verifier on the node's **latest prepared
   candidate** (the newest candidate row for the unit, i.e. `get_candidate`'s
   `ORDER BY id DESC` result); the verdict is recorded.
5. **`land the node`** — on a `pass` verdict, the coordinator immediately calls
   `integrate --node <id>` to merge that one candidate onto the feature branch,
   *then* marks the node `done`. Integrating before marking done is what makes
   `depends_on` meaningful (§11); a final `integrate` sweep is idempotent and
   lands any stragglers.
6. **`integrate` (final)** — merges any remaining prepared candidates onto the
   feature branch (see §6.2), then offers cleanup.
7. **`report`** — deterministic skeleton plus the coordinator's narrative.

The coordinator drives this loop; it is not itself scheduled. It may re-invoke
the planner (`start --replan`) or edit `dag.json` after a failure.

## 6. `intergent` core changes

Four additive changes. A campaign is one plane whose integration branch is the
feature branch, so the campaign wiring needs **no schema migration** — the
exception is §6.4, which extends the verification store:

```bash
git checkout -b feat/nanochat-cpp   # the user chooses the branch
intergent start --no-unit           # the plane adopts it; no branch is created
```

`--no-unit` is a new small flag: it initialises the plane without creating a
worker unit for the coordinator's checkout.

`init_plane(main_branch=, base=)` and `create_workspace(base=...)` already accept
these.

### 6.1 `integrate` (agent-callable)

`integrate` (agent-callable, **without human approval**), restricted to the
plane's `main_branch` (the feature branch):

- select prepared candidates (all, or a node subset);
- order them with the existing `plan_waves` merge planner, then `git merge
  --no-ff` each unit branch onto the feature branch;
- run the plane's trusted checks on the combined tree (fingerprint-cached);
- mark candidates `landed`, units `landed`, release leases, keep branches for
  provenance;
- idempotent: candidates already contained are skipped;
- on merge conflict, abort the merge and return structured findings; never
  leave the feature branch half-merged;
- **safety rail:** refuse when `main_branch` equals the plane's **recorded
  default branch**. That value is captured once, at plane init: the symbolic
  target of `refs/remotes/origin/HEAD` if present, else the branch checked out
  at init, else `init.defaultBranch`, else `main`. Comparing against a value
  recorded at init (not re-derived at integrate time) keeps the rail stable
  across detached HEAD, missing remotes, and default renames. `integrate` is
  therefore **campaign-only by construction**: an ordinary plane's `main_branch`
  is its default branch, so the rail always refuses there; a campaign sets
  `main_branch = feat/…` at init, so it passes (§6.2). Do not "relax" the rail
  to make non-campaign use work — promotion to `master` is the human git step.

```
intergent integrate [--node <id>] [--cleanup none|worktrees|all]
```

`--cleanup` defaults to `none`; interactive cleanup is offered by the
orchestrator, not chosen by the agent (see §7).

### 6.2 Integration branch vs. main

`integrate` lands on the plane's `main_branch`. For a campaign that branch is
the feature branch. Promoting the feature branch to `master` remains a human
git action and is out of scope for the agent surface.

### 6.3 `status` projection and `report`

`intergent status --json` gains the fields the dashboard needs: `feature_branch`, and
per unit `node`, `log`, `candidate`, and `verification`. `intergent report` writes a
deterministic skeleton (design ref, feature branch, nodes, worker ids, commits,
fingerprints/verifications, artifact paths) with an LLM-written "what changed /
risks" section appended.

Adapter lockstep: adding an engine action touches `surface.py` (source of
truth), `integrations/pi/intergent.ts` (`IG_ACTIONS`), `SKILL.md`,
`docs/agents.md`, and `tests/test_skill_package.py`, which asserts the surfaces
stay in sync. The `campaign` tool drives the CLI on top of those verbs. `start`
creating a feature branch stays `human`; `integrate` is `agent`.

### 6.4 Per-node verification fingerprints (fourth change)

§8 wants verifier verdicts reused across re-integration. The existing
`compute_fingerprint` (`verifier.py`) hashes the tree, the **plane's** trusted
check command vector from `config.json`, the toolchain, and the policy — none of
which knows about a node's `acceptance` commands or `tools/gpu.sh`. Reusing a
node verdict by fingerprint therefore requires:

- fingerprinting the node's acceptance command vector (not just plane checks);
- tagging each fingerprint/verification with its source (`plane` vs `node:<id>`)
  so a plane-check fingerprint and a node-acceptance fingerprint cannot collide;
- recording which commands ran (and the GPU tier, if invoked) so a reused
  verdict is auditable.

That is a semantics change to the `fingerprints`/`verifications` store
(`store.py`), not one of the purely additive actions above. If this change is
deferred, §8's reuse claim must be narrowed to **plane-check fingerprints only**
and node verdicts treated as advisory; the narrowed fallback is the cut line if
S0 must stay schema-stable.

## 7. Orchestrator tool surface

One tool, `campaign`, mirroring the shape of the `intergent` CLI. `start` and `plan` are merged.

| Action | Purpose |
|---|---|
| `start <design>` | Adopt the current branch as the feature branch, invoke the planner, write `dag.json`, print the summary. `--replan` re-invokes the planner. |
| `status` | Merge `intergent status --json` with live child state; print the summary. |
| `ready` | Return ready nodes. |
| `spawn <node>` | Create the unit, launch the one-shot worker, tee `worker_<id>.log`. |
| `verify <node>` | Run the verifier on the node's latest prepared candidate; record the verdict. |
| `integrate` | Call `intergent integrate --node <id>` after each pass (per-node landing), plus a final idempotent sweep; then **offer cleanup** (below). |
| `report` | `intergent report` plus the coordinator narrative. |

### Cleanup on integrate

Cleanup is destructive, so the agent cannot silently choose it. `integrate`
first performs the merge; then the extension asks the user with
`ctx.ui.confirm`, e.g.:

```text
Integrate succeeded onto feat/nanochat-cpp.
Remove unit worktrees for this campaign?   [y/N]
```

Choices map to `intergent integrate --cleanup` and to artifact removal:

- **worktrees** — `intergent status --gc` (or `integrate --cleanup worktrees`);
- **artifacts** — delete `<branch-key>.dag.json`, `<branch-key>.state.json`,
  and `<branch-key>.worker_*.log`;
- **`<branch-key>.report.md` is kept** unless the user explicitly asks to
  remove everything.

On a conflict, cleanup is never offered.

## 8. GPU arbitration

`tools/gpu.sh` (flock + foreign-process gate + tiered timeout) is a deliverable
of S1, not existing code. **Only the verifier is given the broker tool**;
workers never touch the GPU, and T0 CPU remains the inner development loop. The
verifier's results are recorded as `intergent` verifications and, once §6.4
lands, re-integration reuses them by fingerprint; until then reuse covers
plane-check fingerprints only.

## 9. Failure and resume

| Failure | Response |
|---|---|
| Worker produces no candidate / fails acceptance | Node `failed`; coordinator retries (bounded), splits the node, or stops. |
| Verifier `fail` | Same as above, with the verifier's findings attached to the retry prompt. |
| `declare` returns `queued` | The DAG let two parallel nodes own the same file. The one-shot worker **exits immediately** (it must not block on a lease); the node returns to `ready`; the coordinator adds a `depends_on` edge to serialize (or re-plans), then re-spawns. Never force a conflict. |
| Merge conflict at `integrate` | Merge aborted; findings surfaced. The node stays `failed`; coordinator spawns a resolver or adds an edge. The feature branch is never left half-merged. |
| Orchestrator crash | Workers are **child processes of the coordinator and are not detached**, so a crash kills them mid-flight. On resume, any node left `running` is reset to `pending` and its worktree reset (partial commits discarded), then re-spawned fresh. `campaign start` reloads `dag.json` + `state.json` + `intergent status`; git and `state.db` win over `state.json`. |
| `dag.json` hand-edited | Coordinator changes (added edges, split nodes) are appended to the event log, so the plan's evolution is auditable alongside commits and fingerprints. |

Caps: `concurrency` (start at 3–4), max attempts per node, and a wall-clock
budget. Each `spawn` is a full agent session; the cost is bounded by the caps,
not by hope.

## 10. Staged implementation

**S0 — `intergent` core.** Add `integrate`, `start --no-unit`, the `status`
fields, and `report`; update the adapter surfaces and `test_skill_package.py`;
add tests; land §6.4 or narrow §8 to plane-check reuse. Acceptance: two unit
candidates on `feat/x` integrate onto `feat/x`; re-running is a no-op; `intergent
report` is deterministic; a campaign bootstrap leaves no phantom unit.

**S1 — orchestrator and agents.** The `campaign` extension (one tool, one-shot
spawning, log tee, dashboard widget), the agents `planner`/`worker`/`verifier`,
and `tools/gpu.sh`. Acceptance: run a three-node DAG derived from `DESIGN.md`;
artifacts land on the feature branch; `report.md` and `worker_<id>.log` are
written; cleanup is offered.

**S2 — resilience.** Resume after kill, retry/split policy, conflict path, caps.
Acceptance: kill mid-run and resume; force a scope conflict and observe
serialization via a new edge.

**Deliberately deferred:** RPC-steerable workers, multiple campaigns per plane,
any human-authored prose plan.

### Implementation map

| Piece | Location |
|---|---|
| `integrate`, `report`, `start --no-unit`, status projection | `intergent/integrate.py`, `intergent/report.py`, `intergent/service.py`, `intergent/surface.py` |
| `dag.json`/`state.json` layout and readers | `intergent/campaign.py` |
| per-node verification fingerprints (§6.4) | `intergent/verifier.py`, `intergent/store.py` |
| `campaign` coordinator tool + widget + log tee | `integrations/pi/campaign.ts` |
| shared pi-extension helpers (CLI resolution, state paths, subagent runner) | `integrations/pi/common.ts` |
| pi package manifest (tools + skill together) | `package.json` |
| planner/worker/verifier agents | `integrations/pi/agents/*.md` |
| GPU broker | `tools/gpu.sh` |
| tests | `tests/test_campaign.py`, `tests/test_cli.py`, `tests/test_skill_package.py` |

## 11. Open questions

- **Resolved:** `integrate` runs **per node, immediately after a `pass` verdict
  and before any dependent spawns**. Batching is not viable: with
  `--base <feature_branch>`, a dependent would start from a base lacking its
  dependencies' code, so its `acceptance` could not pass. A final sweep remains
  for idempotence. Barriers/group aggregation are expressed as explicit nodes
  that every group member depends on, never as implicit batching.
- Shared build files (Bazel `BUILD`, `Cargo.toml`, lockfiles) make overlap the
  common case, not an edge case. Rather than letting `declare`'s `queued` path
  become the main path, the planner must route shared-file edits to an explicit
  **aggregation node** — a node all touched components `depends_on` — which owns
  the shared file. Every other node still declares ownership of its own files.
- Where does feature→`master` promotion happen: a campaign action, or a human
  `git merge`? Current design keeps it a human `git` step.
