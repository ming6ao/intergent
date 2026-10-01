# Local plane reference implementation

> Status: **implemented** as a dependency-free Python reference implementation
> of the local plane and campaign orchestration.

The implementation lives in [`intergent/`](../intergent) with a test suite in
[`tests/`](../tests). Every adapter calls one service layer, matching the
"one engine, many adapters / no adapter owns state" rule in
[Architecture](./architecture.md).

```
CLI:     ./bin/intergent   (or: python -m intergent)
State:   .intergent/state.db  (SQLite, WAL)
Config:  .intergent/config.json
```

## 1. Module map

| Module | Responsibility |
|---|---|
| `intergent/cli.py` | generated `argparse` CLI (`intergent`), human + `--json` output |
| `intergent/surface.py` | **single source of truth**: action registry, validation, dispatch |
| `intergent/service.py` | **single owner of state**: sessions, units, candidates, conformance, integration |
| `intergent/store.py` | SQLite persistence (WAL) |
| `intergent/gitutil.py` | Git plumbing (`worktree`, `merge-tree`, `merge`, `commit`, `branch`, `changed_files`) |
| `intergent/scopes.py` | Directory ownership: normalization, `owns` parsing, subtree conflicts |
| `intergent/verifier.py` | Fingerprints (plane and node sources) and trusted-check runner |
| `intergent/planner.py` | Candidate ordering by DAG wave + combined-tree simulation |
| `intergent/waves.py` | DAG wave projection (directory-subtree packing, `concurrency` cap) |
| `intergent/integrate.py` | Agent-callable feature-branch landing and node verification recording |
| `intergent/commitops.py` | Shared integration primitives (worktree, merge order, landed marking) |
| `intergent/campaign.py` | `dag.json` / `state.json` layout and readers |
| `intergent/report.py` | Deterministic campaign report skeleton |

## 2. What one campaign gets

A coordinator turns a design into a DAG, then for each ready node creates a unit
(its own `git worktree` + branch), launches a one-shot worker, and verifies and
integrates the candidate before any dependent node spawns.

```text
Node ──► Unit (worktree + branch) ──► owns directories (plan-time)
                                          │
                       wave packing: subtree overlap ⇒ later wave
                                          │
                                          ▼
                        edit owned dirs ──► commit (conformance check)
                                          │
                                          ▼
                              prepared candidate
                                          │
                    integrate (--no-ff onto feature branch)
                              │            ▲
                     fingerprint-cached    │ node verdict (source node:<id>)
                       combined checks ────┘
```

`done` means verified **and** integrated, so a dependent's base already contains
its dependencies' code. There is no runtime lease: serialization is entirely a
projection of `owns` + `depends_on`, and a commit that leaves the owned
subtrees is rejected.

## 3. Action reference

Five actions. The CLI renders them as subcommands and the pi `ig` tool mirrors
them (`IG_ACTIONS`, asserted in lockstep); the `campaign` tool drives the CLI's
orchestration loop on top. The engine surface comes from `surface.py`.

### Bootstrap

```bash
intergent start [--agent NAME] [--name N] [--path DIR] [--main main] [--base main]
                [--check NAME=COMMAND ...] [--force] [--no-unit]
```

`start` (alias `init`) is idempotent and the single bootstrap entry point: it
writes `.intergent/config.json` and `.intergent/state.db` (and adds
`.intergent/` to the repo-local `.git/info/exclude`) if the plane is missing,
then creates a unit for the directory unless it is already inside one.
Re-running it from a unit worktree is a no-op. `--no-unit` initialises the plane
without creating a unit, for a campaign coordinator's checkout; `--main` names
the integration/feature branch and **must already exist** — `start` adopts the
current branch and never creates a branch. `--base` records the fork point
(default: the integration branch). The recorded `default_branch` is captured once
at init (origin `HEAD`, else `init.defaultBranch`, else an existing
`main`/`master`, else `main`). The programmatic
plane-only helper is `Service.init_plane(root, ...)`.

### Authoring (workers)

```bash
intergent commit --unit U -m "message" [--summary S]
```

A worker does not declare anything at runtime. Its node's `owns` directories are
part of `dag.json` and are handed to it by the coordinator's `spawn` action.
`commit` runs a **plan-conformance check**: it computes
`git diff --name-only <unit.base_commit> <head>` and rejects the commit if any
changed path lies outside the node's owned directory subtrees. A rejection means
the planner under-declared: the coordinator widens `owns` or adds a `depends_on`
edge, the DAG fingerprint changes, and the waves replan.

Ownership syntax is `dir:PATH` (a bare path is accepted). Non-directory specs
(`file:`, `symbol:`, …) are rejected when the DAG is projected. See
[Conflict engine](./conflict-engine.md).

### Candidates and integration

```bash
intergent commit -m "message" [--summary S]       # commit + register candidate
intergent status --simulate [--no-checks]          # wave plan + combined-tree checks
intergent integrate [--node ID] [--acceptance CMD ...] [--gpu none|T1|T2]
                    [--check-only] [--cleanup none|worktrees|all] [--no-checks]
intergent report [--narrative TEXT] [--design REF]
intergent status [--health] [--gc] [--short] [--unit U]
```

`integrate` is the campaign's agent-callable landing:

- orders the prepared candidates with the wave planner;
- merges each unit branch onto the plane's `main_branch` (the feature branch)
  with `git merge --no-ff` (branches kept for provenance);
- runs the plane's trusted checks on the combined tree (fingerprint-cached);
- marks candidates and units `landed`.

It is idempotent; a merge conflict aborts the merge and returns structured
findings without leaving the feature branch half-merged; combined checks that
fail reset the branch to its pre-merge tip. A safety rail **refuses** to
integrate onto the plane's recorded **default branch**, so promoting a feature
branch to the default branch stays a human `git` step. `--check-only` records a
node's acceptance verdict (fingerprint source `node:<id>`) without merging,
which the orchestrator's verifier uses. `report` writes the deterministic
`<branch-key>.report.md` skeleton plus an optional narrative.

## 4. Semantics implemented

### Directory ownership (`scopes.py`)

- normalize paths (`dir:src/api/` → `src/api`); the repository root is `"."`
  and is an ancestor of every directory;
- `parse_owns` accepts `dir:PATH` / bare paths and rejects every non-directory
  kind, so a plan cannot silently depend on file-level serialization;
- `owns_conflict` returns a reason when two owned sets overlap by subtree
  (equal, ancestor, descendant) on path-segment boundaries; there is no fuzzy
  matching;
- `path_within_owns` backs the commit-time conformance check.

Ownership is entirely plan-time; see [Conflict engine](./conflict-engine.md).

### Verification (`verifier.py`)

`fingerprint = sha256(tree, cmd_digest, toolchain_digest, policy_digest, source)`:

- `tree`: candidate or combined commit tree;
- `cmd_digest`: the command vector — the plane's configured checks, or a node's
  acceptance commands;
- `toolchain_digest`: `git --version`, Python version, and hashed lockfiles
  (`package-lock.json`, `Cargo.lock`, `go.sum`, `poetry.lock`, …);
- `policy_digest`: policy block of the config;
- `source`: `plane` or `node:<id>`, so a plane-check fingerprint and a node
  acceptance fingerprint cannot collide.

Checks run in a clean detached scratch worktree at the commit. A passing
verification for an unchanged fingerprint is reused from cache; verification
itself never mutates the candidate or the feature branch.

### Waves and simulation (`planner.py`, `waves.py`)

There are two wave planners, and they answer different questions:

- **`waves.plan_dag_waves`** is the **scheduler**. Before any work runs, it packs
  DAG nodes into waves from `owns` and `depends_on`, capped by `concurrency`
  (default 3). Directory-subtree overlap forces the later node into a later
  wave. `Service.status()` exposes the projection as `dag_waves`; the
  orchestrator persists it and refuses to spawn a node outside the current wave.
  A changed DAG fingerprint (`id`, `owns`, `depends_on`) triggers a replan.
- **`planner.plan_waves`** groups *committed candidates* by their DAG wave index
  for **integration**. Each wave is materialized as a synthetic combined commit
  and the configured checks run once over the combined tree; textual merge
  conflicts are detected by git during `integrate`.

`integrate` flattens the candidate wave plan into a merge order and advances the
feature branch one `--no-ff` merge at a time, verifying the combined tree after
each merge.

### Integration (`integrate.py`, `commitops.py`)

`integrate` flattens the wave plan into a merge order and advances the feature
branch one `--no-ff` merge at a time, verifying the combined tree after each
merge. `commitops.py` holds the shared primitives (worktree lookup, merge order,
`landed` marking, conflict summary).

## 5. pi tool surface

The pi package registers two tools:

- `campaign` (coordinator): `start`, `status`, `ready`, `spawn`, `verify`,
  `integrate`, `report`.
- `ig` (worker): the engine verbs `start`, `status`, `commit`, `integrate`,
  `report`.

Both are thin forwarders over the CLI (`runIg`), so the engine is never imported
into the agent runtime and no `PATH` install is needed. Both register
`defaultActive: false`, so a plain session neither declares them nor injects
their prompt guidelines; `/skill:intergent <DESIGN.md>` activates them for the
session when the path names an existing design document. `runSubagent` passes
each agent's `tools:` allowlist to `pi --tools`: a worker gets `ig` but never
`campaign`, and the verifier gets neither. The engine's action registry remains
`intergent/surface.py`. See [Agent integration](./agents.md).

## 6. Tests

```bash
python3 -m unittest discover -s tests -v
```

The suite covers directory normalization and subtree conflicts, DAG wave
projection, commit-time plan conformance, and end-to-end flows (campaign
integration and idempotency, conflict atomicity, node verification caching,
failing checks, simulation, cleanup, reporting) plus CLI and packaging smoke
tests.

## 7. Deliberate gaps

- Symbol/AST extraction is not implemented; directory ownership and
  `git merge-tree` are the detectors. Dependency edges beyond `owns`/`depends_on`
  are not inferred.
- No long-lived daemon or unix socket yet: the CLI calls the SQLite service
  directly (WAL).
- `jj` workspaces, sandboxing, and shared dependency caches are not implemented.

Prev: [Local plane](./local-plane.md) · Next: [Agent integration](./agents.md)
