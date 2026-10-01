# Sliceme reference

Action reference, module map, state layout, verification semantics, tests, and
deliberate gaps. For the model and workflow, see [guide.md](./guide.md).

## 1. Actions

The CLI, the pi `sliceme` tool, and the pi `campaign` tool all derive from one action
registry (`sliceme/surface.py`). Six engine verbs:

| Action | Purpose |
|---|---|
| `start` (alias `init`) | Bootstrap the plane and a unit for the current directory (idempotent). |
| `status` | Units, candidates, waves, health, simulation. |
| `commit` | Commit the worktree, enforce plan conformance, register the candidate. |
| `integrate` | Merge verified candidates onto the feature branch; `check_only` records a verdict. |
| `report` | Write the deterministic campaign report plus an optional narrative. |
| `exec` | The single sandboxed executor queue: `submit`/`run`/`wait`/`cancel` check jobs. |

The pi `campaign` tool adds orchestration verbs (`ready`, `spawn`, `verify`) on
top; those drive the engine and the DAG rather than adding engine actions.

### `start`

```bash
sliceme start [--name N] [--path DIR] [--session S] [--kind session|worker]
                [--base REF] [--main BRANCH] [--check NAME=COMMAND ...]
                [--force] [--no-unit]
```

Idempotent bootstrap: writes `.sliceme/config.json` and `.sliceme/state.db`
(and adds `.sliceme/` to the repo-local `.git/info/exclude`) when the plane is
missing, then creates a unit for the directory unless it is already inside one.
Re-running from a unit worktree is a no-op.

- `--main BRANCH` names the integration/feature branch and **must already
  exist**; `start` adopts the current branch and never creates one.
- `--base REF` records the fork point (default: the integration branch).
- `--no-unit` initialises the plane without creating a unit, for a coordinator's
  checkout.
- `--check NAME=COMMAND` registers a trusted plane check (repeatable).
- The recorded `default_branch` is captured once at init: origin `HEAD`, else
  `init.defaultBranch`, else an existing `main`/`master`, else `main`.

The programmatic plane-only helper is `Service.init_plane(root, ...)`.

### `status`

```bash
sliceme status [--unit U] [--short] [--simulate] [--no-checks] [--health] [--gc]
```

`--short` prints only the current unit name. `--health` checks git/config/db.
`--gc` prunes worktrees and landed-unit branches. `--simulate` groups prepared
candidates into DAG waves, materializes each wave's combined tree, and runs the
configured checks once over the combined result; `--no-checks` plans only.

The default projection returns `dag_waves` (the scheduler's wave plan) plus
per-unit campaign columns (`node`, `log`, `candidate`, `verification`).

### `commit`

```bash
sliceme commit [-m "message"] [--unit U] [--summary S]
```

A worker declares nothing at runtime. `commit` runs the **plan-conformance
check**: it computes `git diff --name-only <unit.base_commit> <head>` and
rejects the commit if any changed path lies outside the node's owned directory
subtrees. A rejection means the planner under-declared: the coordinator widens
`owns` or adds a `depends_on` edge, and the waves replan.

Ownership syntax is `dir:PATH` (a bare path is accepted). Non-directory specs
(`file:`, `symbol:`, …) are rejected when the DAG is projected.

### `integrate`

```bash
sliceme integrate [--node ID] [--acceptance CMD ...] [--gpu none|T1|T2]
                    [--check-only] [--cleanup none|worktrees|all] [--no-checks]
```

- Orders prepared candidates with the wave planner.
- Merges each unit branch onto the plane's `main_branch` (the feature branch)
  with `git merge --no-ff`; branches are kept for provenance.
- Runs the plane's trusted checks on the combined tree (fingerprint-cached).
- Marks candidates and units `landed`.

It is idempotent; a merge conflict aborts the merge and returns structured
findings without leaving the feature branch half-merged; combined checks that
fail reset the branch to its pre-merge tip. A safety rail **refuses** to
integrate onto the plane's recorded **default branch**, so promoting a feature
branch to the default branch stays a human `git` step.

`--node ID` lands only that candidate. `--acceptance CMD` runs the node's
acceptance commands (repeatable). `--check-only` records a node's acceptance
verdict (fingerprint source `node:<id>`) without merging; this is what the
orchestrator's verifier uses. `--cleanup` defaults to `none`.

### `report`

```bash
sliceme report [--narrative TEXT] [--design REF]
```

Writes `.sliceme/<branch-key>.report.md`: a deterministic skeleton (design
ref, feature branch, nodes, worker ids, commits, fingerprints/verifications,
artifact paths) with the coordinator's narrative appended under
"What changed / risks".

### `exec`

```bash
sliceme exec [--submit] [--validate] [--gpu-required] [--run] [--wait] [--cancel]
               [--job ID] [--source SRC] [--commit REF] [--command CMD]...
               [--sandbox none|bwrap|unshare] [--gpu none|T1|T2]
               [--priority N] [--timeout SECONDS] [--wave N]
               [--requester ID] [--limit N]
```

The single serialized executor (``sliceme/executor.py``).  Multiple verifiers
delegate to it instead of each running the acceptance suite.

- `--validate`: resolve and validate the project sandbox gate (manifest and,
  with `--gpu-required`, a GPU runner); exits non-zero when the gate fails.
- `--submit`: enqueue a check job for `--source` (e.g. `node:w1`, `wave:0`),
  `--commit`, and one or more `--command`.  A job whose
  `(tree, commands, toolchain, policy, sandbox, source)` fingerprint already
  passed is returned `cached`; the commands are not re-run.
- `--run`: drain the queue with the single runner.  Holds an exclusive `flock`
  on `.sliceme/executor.lock`, so exactly one check vector runs at a time.
- `--wait`: block until `--job` is terminal (or `--timeout`, default 600s).
- `--cancel`: cancel a queued `--job`.
- no flag: print queue counts plus queued/running/recent jobs.

Each job runs in a detached scratch worktree at `--commit`, wrapped in the
resolved sandbox (§4).  The result (status, exit code, output, duration,
fingerprint) is stored in the `jobs` table.  `--run` first recovers any
`running` job whose lease expired.

### Sandbox manifests

The target repository provides how to run checks in isolation as a tracked
manifest at the repo root: `sliceme.sandbox.json`, `.sliceme-sandbox.json`, or
`tools/sliceme-sandbox.json` (never under `.sliceme/`, which is git-excluded).
Schema: `version`, `command` (prefix receiving `/bin/sh -lc "<cmd>"`),
`network`, `readonly_repo`, `writable`, `setup`, and `gpu`
(`{command, tiers}`).

Resolution: `--sandbox` > `dag.json.sandbox` > `policy.sandbox` > discovered
manifest > `none`.  The planner records `"sandbox": {"path": ...}` in
`dag.json`; the coordinator runs `exec --validate` before verifying and records
the `sandbox_digest` in `state.json` and events.  `policy.require_sandbox` (or
`dag.json.sandbox_required`) fails closed when no profile exists.  A manifest
`digest` recorded in `dag.json` is re-checked, so a post-plan manifest change is
rejected.

## 2. Module map

| Module | Responsibility |
|---|---|
| `sliceme/surface.py` | **single source of truth**: action registry, validation, dispatch |
| `sliceme/cli.py` | generated `argparse` CLI (`sliceme`), human + `--json` output |
| `sliceme/service.py` | **single owner of state**: sessions, units, candidates, conformance, integration |
| `sliceme/store.py` | SQLite persistence (WAL) |
| `sliceme/gitutil.py` | Git plumbing (`worktree`, `merge`, `merge-tree`, `commit`, `branch`, `changed_files`) |
| `sliceme/ownership.py` | Directory ownership (normalization, `owns`, subtree conflicts) and the DAG wave projection |
| `sliceme/verifier.py` | Fingerprints (plane and node sources) and the sandboxed trusted-check runner |
| `sliceme/sandbox.py` | Isolation profiles + project manifests (`none`/`bwrap`/`unshare`/`command`), the gate, and command wrapping |
| `sliceme/executor.py` | The single sandboxed executor queue (submit/run/wait/cancel, dedupe, leases) |
| `sliceme/integrate.py` | Feature-branch landing, node verification recording, candidate wave ordering, combined-tree simulation |
| `sliceme/campaign.py` | `dag.json` / `state.json` layout and readers; deterministic report |

The engine is dependency-free Python 3.11+. `Service` is the only state owner;
adapters only parse arguments and render results. `bin/sliceme` is a shim so
the CLI runs without installation.

## 3. State layout

All campaign state lives under `.sliceme/`, **prefixed by the feature-branch
name** so one campaign's files form a single glob and no two campaigns collide.
Let `branch-key` replace `/` with `--` (`feat/x` → `feat--x`):

```text
.sliceme/
  config.json                      # plane config
  state.db                         # units, candidates, fingerprints, verifications, jobs (SQLite, WAL)
  executor.lock                    # exclusive lock held by the single executor runner
  feat--x.dag.json                 # canonical plan (never committed)
  feat--x.state.json               # executor progress (node -> status)
  feat--x.report.md                # final report (kept on cleanup)
  feat--x.worker_<id>.log          # one log per worker id
  worktrees/                       # one git worktree per unit
  scratch/                         # detached simulation worktrees (transient)
```

`state.json` holds only what git and `state.db` cannot express quickly: per-node
`pending|running|done|failed`, the last verdict, and attempt counts. On conflict,
git and `state.db` are authoritative; `state.json` is a rebuildable cache.

SQLite tables: `sessions`, `units`, `candidates`, `fingerprints`,
`verifications`, `jobs`.

## 4. Verification

`fingerprint = sha256(tree, cmd_digest, toolchain_digest, policy_digest, sandbox_digest, executor_digest, source)`:

- `tree`: candidate or combined commit tree;
- `cmd_digest`: the command vector — the plane's configured checks, or a node's
  acceptance commands;
- `toolchain_digest`: `git --version`, Python version, and hashed lockfiles
  (`package-lock.json`, `Cargo.lock`, `go.sum`, `poetry.lock`, …);
- `policy_digest`: policy block of the config;
- `sandbox_digest`: the resolved isolation profile (`sliceme/sandbox.py`), so a
  stricter sandbox invalidates a cached verdict;
- `executor_digest`: the executor semantics version, so changing how checks are
  run invalidates cached verdicts;
- `source`: `plane`, `node:<id>`, or `wave:<n>`, so unrelated verdicts cannot
  collide.

Checks run in a clean detached scratch worktree at the commit and, when a
sandbox is configured, wrapped accordingly. A passing verification for an
unchanged fingerprint is reused from cache; verification never mutates the
candidate or the feature branch. Agent-reported tests are provenance only,
never acceptance.

## 5. Tests

```bash
python3 -m unittest discover -s tests -v
```

The suite covers directory normalization and subtree conflicts, DAG wave
projection, commit-time plan conformance, the executor queue and sandbox
profiles, and end-to-end flows (campaign integration and idempotency, conflict
atomicity, node verification caching, failing checks, simulation, cleanup,
reporting) plus CLI and packaging smoke tests.

| File | Covers |
|---|---|
| `tests/test_scopes.py` | ownership normalization, `owns`, subtree conflicts, conformance path check |
| `tests/test_waves.py` | DAG wave projection, dependency barriers, caps, validation |
| `tests/test_campaign.py` | feature-branch integration, node verification, report, DAG/state layout |
| `tests/test_executor.py` | executor queue, sandbox profiles/wrapping, fingerprint invalidation |
| `tests/test_sandbox_gate.py` | manifest discovery/validation, fail-closed gate, GPU runner, setup |
| `tests/test_e2e.py` | end-to-end conformance and integration flows |
| `tests/test_cli.py` | CLI surface and lifecycle |
| `tests/test_skill_package.py` | pi package contract, tool/action lockstep, docs |

## 6. Deliberate gaps

- Symbol/AST extraction is not implemented; directory ownership and
  `git merge-tree` are the detectors. Dependency edges beyond `owns`/`depends_on`
  are not inferred.
- No long-lived daemon or unix socket: the CLI calls the SQLite service directly
  (WAL).
- One campaign per plane; RPC-steerable workers and multiple concurrent
  campaigns are out of scope.
- `jj` workspaces and shared dependency caches are not implemented.  The
  sandbox abstraction, project manifest, and coordinator gate are implemented
  (Phase 1–2, see `docs/guide.md` §6); the one-worktree-per-wave recorder and
  the `campaign exec` orchestration are still to come (Phase 3–4).
- Promotion from the feature branch to the default branch is a human `git` step.
