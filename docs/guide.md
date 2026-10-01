# Sliceme guide

Sliceme *(slice the design into parallel agents)* coordinates parallel coding
agents around one campaign: a machine-readable DAG, a `git worktree` per worker,
plan-time **directory ownership**, and fingerprint-pinned integration onto a
feature
branch. This guide covers the model, ownership, the orchestration lifecycle, and
the pi agent integration. The command/action reference lives in
[reference.md](./reference.md).

## 1. The problem

Several agents working the same repository collide in ways Git is blind to: they
edit different files that depend on each other, or plan contradictory changes.
The failure modes are *authoring conflicts* (wasted, contradictory work) and
*integration conflicts* (stale-tip breakage, CI churn).

Git compares *text*, not *intent*, and landing is per-branch rather than ordered
by a dependency graph. Sliceme adds a deterministic layer over Git:

- isolates each worker in a worktree;
- assigns each DAG node disjoint **directories** at plan time;
- serializes overlapping directory subtrees into waves;
- verifies each candidate against a content fingerprint;
- integrates verified candidates one node at a time onto a campaign feature
  branch.

LLMs draft the plan; they never decide at runtime whether something blocks.

### Non-goals

- Replacing Git. Git remains the source of truth.
- Running models. The coordinator spawns the client's own headless mode.
- Resolving arbitrary text conflicts or providing a review UI.
- A remote/shared scheduler or multiple concurrent campaigns per plane.

## 2. Core concepts

| Term | Definition |
|---|---|
| **Campaign** | One feature branch plus a `dag.json` plan and executor `state.json`. |
| **Coordinator** | The top-level session that owns the plan and drives the campaign. |
| **Node** | One DAG unit of work with `owns`, `depends_on`, `acceptance`, `gpu`. |
| **Unit** | An isolated writer: a worktree + branch (`sliceme/<name>`). |
| **Ownership** | The repo-relative **directories** a node may change (`dir:` only), compared by subtree overlap. |
| **Conformance** | Commit-time check that every changed path lies inside the node's owned directories. |
| **Candidate** | A committed unit awaiting verification and integration. |
| **Wave** | A derived batch of nodes with disjoint owned subtrees that may run concurrently; also the integration order. |
| **Fingerprint** | Content hash of (commit tree, command vector, toolchain, policy, source) that pins a verification result. |

## 3. Directory ownership

Ownership is decided entirely at plan time. There is no runtime declare step, no
lease, and no operation taxonomy.

Every DAG node declares `owns`: repo-relative **directories**. A node must name
the deepest directory that contains each path it will add, modify, or delete.

```jsonc
{ "id": "w1", "owns": ["dir:src/api", "dir:src/api/v1"], "depends_on": [] }
```

Normalization:

| Input | Canonical |
|---|---|
| `dir:src/api` | `src/api` |
| `src/api/` | `src/api` |
| `dir:.`, ``, `/` | `.` (the repository root) |

Non-directory specs — `file:`, `symbol:`, `api:`, `schema:`, `config:`,
`migration:`, `infra:`, `test:` — are rejected when the DAG is projected. A plan
that tries to own a single file fails loudly rather than silently receiving
directory-level serialization.

### The conflict rule

Ownership is a **subtree**. Two nodes conflict when one owned directory is equal
to, an ancestor of, or a descendant of the other's, compared on path-segment
boundaries. The root `.` is an ancestor of every directory, so a node that owns
`dir:.` serializes against every other node.

```
src/api       vs src/api        -> conflict (equal)
src           vs src/api        -> conflict (ancestor)
src/api       vs src/api/v1     -> conflict (descendant)
src/api       vs src/service    -> ok       (siblings)
src/models    vs src/model      -> ok       (no token similarity tier)
```

There is deliberately no fuzzy matching: concurrency is explainable from the
`owns` sets alone.

### Conformance: the runtime guarantee

Because there are no leases, the guarantee is enforced after the worker commits:

```text
changed = git diff --name-only <unit.base_commit> <head>
violations = [p for p in changed if p not in the subtree of any owned dir]
```

Any violation raises an error and the candidate is not registered. The
coordinator then widens the node's `owns` or adds a `depends_on` edge and
re-spawns. This keeps "no two same-wave units touch the same directory"
auditable without runtime locking.

### Authoring guidance

- Own the **deepest** directory that contains the work; owning a parent
  serializes its whole subtree.
- Keep same-wave `owns` disjoint.
- Route shared build files (`BUILD`, `Cargo.toml`, lockfiles) to an explicit
  **aggregation node** that every touched component `depends_on`; that node owns
  the shared directory (`dir:.` for root files).
- Express ordering that same-directory serialization does not already give you
  with `depends_on`, never by hoping for a runtime queue.

## 4. The plan: `dag.json` and derived waves

`dag.json` is canonical; there is no `plan.md`. It lives under the git-excluded
`.sliceme/` directory and is never committed.

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
      "owns": ["dir:backends/cpu", "dir:src"],
      "depends_on": [],
      "acceptance": ["bazel test //... --test_tag_filters=-gpu"],
      "gpu": "none" }
  ]
}
```

| Field | Meaning |
|---|---|
| `id` | Worker id and unit name; also the log suffix (`worker_<id>.log`). |
| `label` | Human label for the report and dashboard. |
| `phase` | **Display/report grouping only.** Never used for scheduling. |
| `goal` | The prompt seed handed to the worker. |
| `owns` | Directories the node owns, at the deepest subdirectory that contains each path (`dir:src/api`). Directory subtrees are the only conflict unit. |
| `depends_on` | Node ids that must be `done` before this node is `ready`. |
| `acceptance` | Commands the worker must run and the verifier re-runs. |
| `gpu` | `none`, `T1`, or `T2`; only the verifier may use it. |
| `concurrency` | Per-wave size cap. Defaults to 3 when absent. |

**Phases never schedule; waves do — and waves are derived.** A graph plus the
rules

```text
wave(n)  :=  max(wave(d) + 1 for d in n.depends_on), then earliest wave with
             room (<= concurrency) and no directory-subtree conflict
ready(n) :=  every d in n.depends_on is done AND n is in the current wave
```

are the whole executor. `done` means **verified *and* integrated** onto the
feature branch, so a later wave's `--base <feature_branch>` checkout already
contains the previous wave's code. Batching integration until the end would make
`depends_on` meaningless.

## 5. Orchestration

One top-level **coordinator** turns a design into landed work by spawning a
planner, workers, and a verifier, while the `sliceme` engine remains the
deterministic state, isolation, and integration layer.

```text
USER
 │  launches pi in the repository, on the feature-branch checkout
 ▼
COORDINATOR  (top-level pi session — the session the user sees)
 │  tools: campaign start | status | ready | spawn | verify | integrate | report
 ├── PLANNER   (subagent, invoked by the coordinator via `start`)
 │               reads the design, writes dag.json (plane state, not committed)
 ├── WORKER_*  (one-shot subagent per ready DAG node, own sliceme worktree)
 │               edit owned dirs -> acceptance tests -> commit  (never GPU)
 └── VERIFIER  (read-only subagent, invoked per candidate)
                 T0 CPU then tools/gpu.sh; returns a verdict; never edits
```

The coordinator is **not** an `sliceme` unit: plane bootstrap uses
`start --no-unit`, so campaign setup leaves no phantom unit in the coordinator's
checkout.

### Lifecycle

```text
campaign start <design>     # adopt current branch, planner -> dag.json + waves
        │
        ▼
wave N ready ──► spawn (<= concurrency) ──► worker commits candidate
  ▲                                              │
  │                                              ▼
  │                                verify (verifier: T0, then gpu.sh)
  │                                              │ pass
  │                                              ▼
  │                       node done ◄── integrate --node <id>
  │                                              │
  │            all wave N members done ──► ask cleanup once ──► open wave N+1
  ▼
all nodes done or stopped
        │
        ▼
integrate (idempotent sweep) ──► final cleanup ──► report
```

1. **`start`** adopts the currently checked-out branch as the feature branch (it
   never creates one; starting on the repository default branch is refused),
   bootstraps the plane (`start --no-unit`), and invokes the planner. The planner
   writes `dag.json`; the coordinator projects it into waves and stops for an
   optional human review.
2. **`ready`** returns the current wave's nodes whose dependencies are all
   `done`.
3. **`spawn`** only starts a node in the current wave. It creates a unit
   (`--base <feature_branch>`) and launches a one-shot worker in that worktree.
   Because the previous wave was integrated first, the base already contains it.
4. **`verify`** runs the read-only verifier on the node's latest prepared
   candidate and records the verdict.
5. **`integrate --node <id>`** lands that one verified candidate, then the node
   is marked `done`. When the last member of a wave lands, the next wave opens.
6. **`integrate` (final)** merges any remaining prepared candidates.
7. **`report`** writes the deterministic skeleton plus the coordinator's
   narrative.

The coordinator may re-invoke the planner or edit `dag.json` after a failure. A
coordinator-added `depends_on` edge (or a widened `owns`) changes the DAG
fingerprint, so the next `status`/`ready`/`spawn` reprojects the waves.

### Cleanup per wave

Cleanup is destructive, so the agent cannot silently choose it. When the last
member of a wave lands, the extension asks the user once for that wave; the
wave's `cleanup_done` flag is persisted so the prompt never repeats. Accepting
maps to `integrate --cleanup worktrees` and removes that wave's
`worker_<id>.log` artifacts. The report is kept unless the user explicitly asks
to remove everything. On a conflict, cleanup is never offered.

### GPU arbitration

`tools/gpu.sh` (flock + foreign-process gate + tiered timeout) is the GPU broker.
**Only the verifier is given it**; workers never touch the GPU, and T0 CPU
remains the inner development loop. A busy device returns exit 75, reported as a
retryable failure rather than a code failure.

### Failure and resume

| Failure | Response |
|---|---|
| Worker produces no candidate / fails acceptance | Node `failed`; coordinator retries (bounded), splits the node, or stops. |
| Verifier `fail` | Same as above, with the verifier's findings attached to the retry prompt. |
| `commit` changes a path outside the node's `owns` | The commit is rejected; the coordinator widens `owns` or adds a `depends_on` edge and re-spawns. |
| Merge conflict at `integrate` | Merge aborted; findings surfaced. The node stays `failed`; the coordinator spawns a resolver or adds an edge. The feature branch is never left half-merged. |
| Orchestrator crash | Workers are child processes of the coordinator and are not detached, so a crash kills them. On resume, any node left `running` is reset to `pending` and its worktree reset, then re-spawned. Git and `state.db` win over `state.json`. |

Caps: `concurrency`, max attempts per node, and a wall-clock budget bound the
cost of each spawn.

## 6. Agent integration (pi)

pi is the supported agent harness. The repository is a **pi package** that ships
the `campaign` coordinator tool, the `sliceme` worker tool, and the bundled skill.

```bash
pi install ./                                       # local checkout
# pi install git:github.com/ming6ao/sliceme
# pi install npm:sliceme
pi                                                  # launch the coordinator
```

Both tools register **inactive**: a plain session never lists them nor receives
their prompt guidelines, so Sliceme is only used when asked for.
`/skill:sliceme <DESIGN.md>` is the single entry point: when the argument is
an existing design document, the skill turns `campaign` and `sliceme` on for that
session. There is no single-agent bootstrap — a session is only bound to a unit
when the campaign `spawn` action (or the user) creates one.

| Role | Bound to a unit? | Contract |
|---|---|---|
| **Coordinator** | no | owns the plan (`dag.json`), spawns agents, calls `integrate` after a pass, writes the report |
| **Planner** | no | reads the design, writes `dag.json` |
| **Worker** | yes (its worktree) | edit owned directories → acceptance (CPU) → `commit` |
| **Verifier** | no (read-only) | runs acceptance (T0 then `tools/gpu.sh`), returns a verdict, never edits |

`runSubagent` passes each agent's `tools:` allowlist to `pi --tools`, so a
worker gets `sliceme` but never `campaign`, and the verifier gets no Sliceme tool
at all.

### Worker contract

1. `sliceme` action `status` with `short: true` must succeed — you are in your unit
   worktree.
2. Edit only files under the directories your DAG node owns. `commit` enforces
   this: a changed path outside the owned directories is rejected.
3. Run the node's acceptance commands (CPU only; never the GPU).
4. `commit` and stop. Workers never run `integrate` or `git merge`.

### Coordinator

Start the coordinator with `/skill:sliceme <DESIGN.md>` in pi, then use the
`campaign` tool — or drive the CLI directly: `start --no-unit` (which adopts the
current branch; check out your branch first), then `spawn`/`verify`/`integrate`
per ready node, a final idempotent `integrate` sweep, and `report`.
