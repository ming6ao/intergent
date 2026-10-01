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
                 submits checks to the executor; returns a verdict; never edits
```

The coordinator is **not** an `sliceme` unit: plane bootstrap uses
`start --no-unit`, so campaign setup leaves no phantom unit in the coordinator's
checkout.

The verifier never runs commands itself: it delegates to the single sandboxed
**executor** (§6), which owns the check queue, the GPU broker, and isolation.

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
   candidate. The verifier submits the node's acceptance vector to the
   executor (§6) and records the verdict from its evidence.
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
It is **sliceme's**, shipped with the package and invoked by resolved path;
**only the executor uses it**, workers never touch the GPU, and T0 CPU remains
the inner development loop. A busy device returns exit 75, reported as a
retryable failure rather than a code failure. See §6.3.

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

## 6. Executor, sandbox, and the target wave design

### 6.1 The single executor queue (Phase 1, implemented)

Multiple verifiers never run commands themselves. They submit **check jobs** to
one executor:

```text
VERIFIER A ─┐  submit(job)                    ┌─ wait/notify ─▶ VERIFIER A
VERIFIER B ─┼────────────▶ EXECUTOR QUEUE ────┼─ wait/notify ─▶ VERIFIER B
VERIFIER C ─┘   (SQLite `jobs`, 1 runner)     └─ wait/notify ─▶ VERIFIER C
```

- `sliceme exec --submit --source node:w1 --commit <sha> --command "<cmd>"` enqueues.
- `sliceme exec --run` opens the single-executor lock and drains the queue.
- `sliceme exec --wait --job <id>` blocks until the job is terminal.
- `sliceme exec --cancel --job <id>` cancels a queued job.
- `sliceme exec` with no flags prints the queue status.

Semantics:

- **One runner.** `run`/`drain` hold an exclusive `flock` on
  `.sliceme/executor.lock`, so no two check vectors run concurrently.
- **Dedupe by fingerprint.** A submit whose `(tree, commands, toolchain, policy,
  sandbox, source)` fingerprint already passed returns the cached job; the
  commands are not re-run.
- **Sandboxed.** Each job carries a `sandbox` profile; the executor resolves it
  and every command is wrapped (see 6.2).
- **Crash-safe.** A `running` job whose lease expired is reset to `queued`
  before a drain.

Jobs are recorded in the `jobs` table (`docs/reference.md` §3) with their
command vector, sandbox digest, fingerprint, exit code, output, and timing.

### 6.2 Sandbox profiles and project manifests (Phase 1–2)

`sliceme/sandbox.py` defines a `Sandbox`: a built-in mode (`none`, `bwrap`,
`unshare`) or a project `command` prefix, plus network/read-only/writable
policy, `setup` commands, and an optional `gpu` runner. `Sandbox.digest()` is
folded into the verification fingerprint, so tightening isolation or changing
`setup` invalidates cached verdicts.

Resolution precedence: explicit `--sandbox` > `dag.json.sandbox` >
`policy.sandbox` > discovered project manifest > `none`. `none` is unsandboxed;
`policy.require_sandbox` (or `dag.json.sandbox_required`) makes the gate fail
closed when no profile exists.

The **target repository owns how to run tests in isolation** through a tracked
manifest (`sliceme.sandbox.json`, `.sliceme-sandbox.json`, or
`tools/sliceme-sandbox.json` -- **not** under `.sliceme/`, which is git-excluded):

```jsonc
{
  "version": 1,
  "command": ["tools/run-in-sandbox.sh", "--"],   // receives /bin/sh -lc "<cmd>"
  "network": false,
  "readonly_repo": true,
  "writable": ["/tmp", ".cache"],
  "setup": ["tools/setup-deps.sh"],               // once per snapshot, before acceptance
  "gpu": { "command": ["sliceme-gpu", "--tier", "{tier}", "--"] }
}
```

The **planner** locates the manifest and records `"sandbox": {"path": ...}` in
`dag.json` (or `"sandbox_required": true` when the project needs isolation but
ships no manifest).  The **coordinator** validates the gate with
`sliceme exec --validate` before spawning or verifying, records the resolved
`sandbox_digest` in `state.json` and the campaign event log, and refuses to
continue on failure -- so the sandbox is present before any verifier runs.  A
manifest that changes after the plan is pinned is rejected by digest.

### 6.3 GPU broker ownership

`tools/gpu.sh` is **sliceme's** broker: a host `flock`, a foreign-process gate,
and a tiered timeout. It is shipped with the package and invoked by resolved
path, not as `tools/gpu.sh` relative to the target. The executor composes it
outside the project sandbox for GPU jobs:

```text
executor → sliceme gpu broker (host lock) → project sandbox → acceptance
```

A project may override the GPU invocation through `gpu.command` in its manifest.

### 6.4 Target design: one worktree per wave

Because same-wave nodes own **disjoint directory subtrees**, they cannot author
a file collision, so the per-node worktree is isolation the conflict rule
already guarantees. The target design makes the wave the isolation and
integration unit:

```text
wave open N   -> ONE branch sliceme/wave-<N> + worktree off the feature tip
spawn         -> all ready workers of wave N into that worktree (pure editors)
exec record   -> conformance-by-ownership -> per-node commits (serialized)
exec verify   -> verifiers submit command vectors; the single executor runs them
integrate     -> merge sliceme/wave-<N> --no-ff; mark nodes done; open N+1
```

Workers become pure editors: they never run `git add/commit` (the shared index
is not multi-process safe) and never run the suite in the shared tree. The
executor snapshots the wave tree into a detached scratch worktree and runs the
combined acceptance vector there, so concurrent edits cannot invalidate a run.
Multiple read-only verifiers judge the executor's recorded evidence. Per-node
commits preserve retry and provenance, and a failed node blocks the wave merge.

## 7. Agent integration (pi)

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
| **Verifier** | no (read-only) | submits acceptance to the executor, judges the recorded evidence, never edits |

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
