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
| `intergent/service.py` | **single owner of state**: sessions, units, intents, leases, candidates, integration |
| `intergent/store.py` | SQLite persistence (WAL) |
| `intergent/gitutil.py` | Git plumbing (`worktree`, `merge-tree`, `merge`, `commit`, `branch`) |
| `intergent/scopes.py` | Scope parsing, canonicalization, and the scope tree |
| `intergent/locks.py` | IS/IX/S/SIX/X compatibility matrix and requirement closure |
| `intergent/conflict.py` | Deterministic conflict rules `FM-C001..C003` and matching tiers |
| `intergent/verifier.py` | Fingerprints (plane and node sources) and trusted-check runner |
| `intergent/planner.py` | Greedy wave packing + combined-tree simulation |
| `intergent/integrate.py` | Agent-callable feature-branch landing and node verification recording |
| `intergent/commitops.py` | Shared integration primitives (worktree, merge order, landed marking) |
| `intergent/campaign.py` | `dag.json` / `state.json` layout and readers |
| `intergent/report.py` | Deterministic campaign report skeleton |

## 2. What one campaign gets

A coordinator turns a design into a DAG, then for each ready node creates a unit
(its own `git worktree` + branch), launches a one-shot worker, and verifies and
integrates the candidate before any dependent node spawns.

```text
Node ──► Unit (worktree + branch) ──► Intent (scopes + operation)
                                          │
                         ┌────────────────┴────────────────┐
                  granted │                          queued │ needs_decision
                 (leases) │                                 │
                          ▼                                 ▼
                commit ──► prepared candidate        worker exits; coordinator
                                    │                 re-plans / serializes
                                    ▼
                    integrate (--no-ff onto feature branch)
                              │            ▲
                     fingerprint-cached    │ node verdict (source node:<id>)
                       combined checks ────┘
```

`done` means verified **and** integrated, so a dependent's base already contains
its dependencies' code.

## 3. Action reference

Six actions. The CLI renders them as subcommands and the pi `ig` tool mirrors
them (`IG_ACTIONS`, asserted in lockstep); the `campaign` tool drives the CLI's
orchestration loop on top. The engine surface comes from `surface.py`.

### Bootstrap

```bash
intergent start [--agent NAME] [--name N] [--path DIR] [--main main] [--base main]
                [--check NAME=COMMAND ...] [--lease-ttl 1800] [--force] [--no-unit]
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
intergent declare --unit U --operation OP --scope "KIND:KEY[=OP]" [--scope ...]
intergent declare --unit U --dry-run --operation OP --scope "KIND:KEY"   # check only
intergent declare --unit U --renew                                       # heartbeat
intergent declare --unit U --release                                     # release leases
intergent commit --unit U -m "message" [--summary S]
```

Agents and sessions are created implicitly: `start` registers the agent/session
named by `--agent`/`--session`.

Scope syntax is `kind:key[=operation]`, e.g. `file:src/app.py=modify`,
`symbol:src/app.py#Login.run=replace`, `config:app.timeout=extend`. The
intent-level `--operation` is the default per scope.

`declare` returns one of:

- `granted` — leases acquired; the worker may edit;
- `queued` — `{position, blocker, blocker_node, eta_seconds}`; the worker
  **exits immediately** and reports the blocker (the coordinator serializes the
  node or re-plans); never block and never force a conflict;
- `needs_decision` — destructive-vs-additive on an exact scope; the worker stops
  and the coordinator re-plans.

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
- marks candidates and units `landed` and releases their leases.

It is idempotent; a merge conflict aborts the merge and returns structured
findings without leaving the feature branch half-merged; combined checks that
fail reset the branch to its pre-merge tip. A safety rail **refuses** to
integrate onto the plane's recorded **default branch**, so promoting a feature
branch to the default branch stays a human `git` step. `--check-only` records a
node's acceptance verdict (fingerprint source `node:<id>`) without merging,
which the orchestrator's verifier uses. `report` writes the deterministic
`<branch-key>.report.md` skeleton plus an optional narrative.

## 4. Semantics implemented

### Scope canonicalization (`scopes.py`)

- case-fold, path-clean, strip `./`, de-duplicate;
- `dir` scopes carry a trailing slash; a file's basename is never treated as a
  directory ancestor;
- symbol scopes are `path#Symbol::member`, with the enclosing class as a parent;
- `api` scopes normalize `METHOD /path`;
- kinds: `dir, file, symbol, api, schema, config, migration, infra, test`.

### Hierarchical leases (`locks.py`)

Requirement closure computes a lock mode for every node on the scope-tree path
from `root` to each declared scope:

- additive (`add`/`extend`/`modify`) → `S` on the declared scope, `IS` on
  ancestors;
- destructive (`replace`/`remove`/`rename`/`migrate`) → `X` on the declared
  scope, `IX` on ancestors;
- a node that is both shared and exclusive-intention becomes `SIX`.

Compatibility is the design matrix. A request is granted only when its closure
is compatible with every granted claim of other units; otherwise it is queued
FIFO behind the blocker. `root-to-leaf` closure + per-node compatibility makes
acquisition order-independent, so there is no deadlock.

### Conflict rules (`conflict.py`)

| Rule | Condition | Severity |
|---|---|---|
| `FM-C001 destructive_vs_additive` | one destructive, one additive, overlapping | HIGH if exact/asserted, else MEDIUM |
| `FM-C002 divergent_rewrite` | both destructive, overlapping | HIGH if exact/asserted, else MEDIUM |
| `FM-C003 shared_contract` | both additive, overlapping | MEDIUM if exact, else LOW |

Only asserted `FM-C001` requires a decision (`requires_decision`) and returns
`needs_decision`. `FM-C001`/`FM-C002` also stop two candidates from sharing a
wave. No model is in the verdict path.

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
verification for an unchanged fingerprint is reused from cache. A pass releases
the unit's leases and promotes queued waiters; a failure keeps them.

### Waves and simulation (`planner.py`)

Candidates are packed greedily by real mergeability. A pair is not co-waved if
`git merge-tree` conflicts or if `evaluate` reports `FM-C001`/`FM-C002`;
lease-ordering dependencies force the waiter into a later wave. Each wave is
materialized as a synthetic combined commit and the configured checks run once
over the combined tree.

### Integration (`integrate.py`, `commitops.py`)

`integrate` flattens the wave plan into a merge order and advances the feature
branch one `--no-ff` merge at a time, verifying the combined tree after each
merge. `commitops.py` holds the shared primitives (worktree lookup, merge order,
`landed` marking, conflict summary).

## 5. pi tool surface

The pi package registers two tools:

- `campaign` (coordinator): `start`, `status`, `ready`, `spawn`, `verify`,
  `integrate`, `report`.
- `ig` (worker): the engine verbs `start`, `status`, `declare`, `commit`,
  `integrate`, `report`.

Both are thin forwarders over the CLI (`runIg`), so the engine is never imported
into the agent runtime and no `PATH` install is needed. `runSubagent` passes each
agent's `tools:` allowlist to `pi --tools`: a worker gets `ig` but never
`campaign`, and the verifier gets neither. The engine's action registry remains
`intergent/surface.py`. See [Agent integration](./agents.md).

## 6. Tests

```bash
python3 -m unittest discover -s tests -v
```

The suite covers scope canonicalization/hierarchy, the lock matrix and closure,
conflict rules, and end-to-end flows (campaign integration and idempotency,
conflict atomicity, node verification caching, queueing and promotion, expired
leases, failing checks, simulation, cleanup, reporting) plus CLI and packaging
smoke tests.

## 7. Deliberate gaps

- Symbol/AST extraction is not yet wired to `tree-sitter`; declared scopes and
  `git merge-tree` are the detectors. Dependency edges beyond declared intent
  are not inferred.
- No long-lived daemon or unix socket yet: the CLI calls the SQLite service
  directly (WAL). Lease expiry is reaped lazily on the next call.
- `jj` workspaces, sandboxing, and shared dependency caches are not implemented.

Prev: [Local plane](./local-plane.md) · Next: [Agent integration](./agents.md)
