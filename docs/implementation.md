# Local plane reference implementation

> Status: **implemented** as a dependency-free Python reference implementation
> of the local plane. The production stack in
> [Operations](./operations.md#2-tech-stack) remains Rust (primary) / Go
> (alternative); this tree exists so Phase 0 behaviour can be exercised,
> reviewed, and tested end to end before the systems implementation lands.

The implementation lives in [`intergent/`](../intergent) with a test suite in
[`tests/`](../tests). Every adapter calls one service layer, matching the
"one engine, many adapters / no adapter owns state" rule in
[Architecture](./architecture.md#one-engine-many-adapters).

```
CLI:     ./bin/intergent   (or: python -m intergent, or console script after pip install -e .)
Alias:   ig
MCP:     intergent mcp     (stdio, newline-delimited JSON-RPC)
State:   .intergent/state.db  (SQLite, WAL)
Config:  .intergent/config.json
```

## 1. Module map

| Module | Responsibility |
|---|---|
| `intergent/cli.py` | `argparse` CLI (`intergent` / `ig`), human + `--json` output |
| `intergent/mcp.py` | MCP stdio server exposing the agent hot loop |
| `intergent/service.py` | **Single owner of state**: sessions, units, intents, leases, candidates, landing |
| `intergent/store.py` | SQLite persistence (schema mirrors `docs/operations.md`) |
| `intergent/gitutil.py` | Git plumbing (`worktree`, `merge-tree`, `merge`, `rebase`, `commit-tree`) |
| `intergent/scopes.py` | Scope parsing, canonicalization, and the scope tree |
| `intergent/locks.py` | IS/IX/S/SIX/X compatibility matrix and requirement closure |
| `intergent/conflict.py` | Deterministic conflict rules `FM-C001..C003` and matching tiers |
| `intergent/verifier.py` | Fingerprint computation and trusted-check runner |
| `intergent/planner.py` | Greedy wave packing + combined-tree simulation |
| `intergent/landing.py` | Transactional, approval-gated merge into the local main branch |

## 2. What one user gets

A single user runs several coding-agent sessions. Each session (or worker) gets
its own `git worktree` + branch over one object store, so working trees never
collide. Declared intent is reconciled through hierarchical scope leases, so
agents that would contradict each other are serialized or asked to decide.
Every candidate is verified at its exact commit, the combined result is
simulated before submission, and approved candidates are merged into the local
main branch in dependency/wave order.

```text
Session ──► Unit (worktree + branch) ──► Intent (scopes + operation)
                                             │
                            ┌────────────────┴────────────────┐
                     granted │                          queued │ needs_decision
                    (leases) │                                 │
                             ▼                                 ▼
                 commit → finish → verify ──► candidate ──► simulate waves
                                                              │
                                                    approve ──► land → main
```

## 3. CLI reference

### Bootstrap

```bash
intergent init [--main main] [--base main] [--check NAME=COMMAND ...] [--lease-ttl 1800]
intergent doctor
intergent mcp
```

`init` writes `.intergent/config.json` and `.intergent/state.db` and adds
`.intergent/` to the repo-local `.git/info/exclude` so the main worktree stays
clean.

### Authoring (agents)

```bash
intergent agent register NAME [--model M] [--parent NAME]
intergent session create NAME [--task T] [--attachment terminal|tmux|background]
intergent workspace create NAME [--session S] [--kind session|worker] [--base REF] [--agent NAME]
intergent workspace list | show UNIT

intergent declare --unit U --operation OP --scope "KIND:KEY[=OP]" [--scope ...]
intergent check   --unit U --operation OP --scope "KIND:KEY"        # dry run
intergent heartbeat --unit U
intergent release   --unit U
intergent decide INTENT_ID --action wait|override|redesign [--reason R]
intergent override --unit U --reason R                              # audited
intergent rebase --unit U [--onto REF]
```

Scope syntax is `kind:key[=operation]`, e.g. `file:src/app.py=modify`,
`symbol:src/app.py#Login.run=replace`, `config:app.timeout=extend`. The
intent-level `--operation` is the default per scope.

`declare` returns one of:

- `granted` — leases acquired; the agent may edit;
- `queued` — `{position, blocker, blocker_node, eta_seconds}`; the agent may
  wait, switch to non-conflicting work, or proceed optimistically and rebase;
- `needs_decision` — destructive-vs-additive on an exact scope; choose `wait`,
  `redesign`, or `override` (audited).

### Candidate lifecycle

```bash
intergent commit --unit U -m "message"
intergent finish --unit U [--summary S]        # records the candidate
intergent verify CANDIDATE [--force]           # fingerprint-pinned checks
intergent simulate [--no-checks]               # wave plan + combined-tree checks
intergent review CANDIDATE                     # review packet
intergent approve CANDIDATE [--reason R]
intergent reject  CANDIDATE [--reason R]
intergent land [CANDIDATE ...] [--all] [--no-checks] [--cleanup]
intergent status
intergent gc
```

`land` is transactional per candidate: the merge is materialized and verified in
a scratch worktree first, then applied to the real main worktree. If a merge
conflicts or checks fail, main is left untouched and the candidate is marked
`blocked`/`failed`.

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

Only asserted `FM-C001` requires an explicit decision (`requires_decision`).
LLM-derived findings are represented as `source="inferred"` and are capped
below HIGH; no model is in the verdict path.

### Verification (`verifier.py`)

`fingerprint = sha256(tree, cmd_digest, toolchain_digest, policy_digest)`:

- `tree`: candidate commit tree;
- `cmd_digest`: configured check vector;
- `toolchain_digest`: `git --version`, Python version, and hashed lockfiles
  (`package-lock.json`, `Cargo.lock`, `go.sum`, `poetry.lock`, …);
- `policy_digest`: policy block of the config.

Checks run in a clean detached scratch worktree at the candidate commit. A
passing verification for an unchanged fingerprint is reused from cache. A pass
releases the unit's leases and promotes queued waiters; a failure keeps them.

### Waves and simulation (`planner.py`)

Candidates are packed greedily by real mergeability. A pair is not co-waved if
`git merge-tree` conflicts or if `evaluate` reports `FM-C001`/`FM-C002`;
lease-ordering dependencies force the waiter into a later wave. Each wave is
materialized as a synthetic combined commit and the configured checks run once
over the combined tree.

### Landing (`landing.py`)

Approved candidates are ordered by wave, then landed one at a time. For each:
trial-merge into a detached scratch worktree at the current main tip, run
checks, then perform the real merge in the main worktree. The merged result is
recorded as additional verification evidence. Landing stops at the first
failure so main and later candidates stay consistent.

## 5. MCP tool surface

`register_agent`, `create_workspace`, `register_child`, `declare_intent`,
`check_conflicts`, `claim_scope` (alias of `declare_intent`), `heartbeat`,
`release`, `commit_workspace`, `finish_workspace`, `verify`, `status`,
`current_workspace`, `submit` (builds the review packet; landing still requires
human approval). `unit` is optional when the server runs inside a unit
worktree. For Claude Code, pi, and generic agents see
[Agent integration](./agents.md).

## 6. Tests

```bash
python3 -m unittest discover -s tests -v
```

The suite covers scope canonicalization/hierarchy, the lock matrix and closure,
conflict rules, and end-to-end local-plane flows (happy-path landing, queueing
and promotion, needs-decision/override, expired leases, failing checks,
fingerprint caching, simulation, textual-conflict blocking, cleanup) plus CLI
and MCP smoke tests.

## 7. Deliberate gaps (next phases)

- Symbol/AST extraction is not yet wired to `tree-sitter`; declared scopes and
  `git merge-tree` are the detectors. Dependency edges beyond declared intent
  are not inferred.
- No long-lived daemon or unix socket yet: the CLI/MCP call the SQLite service
  directly (WAL). Lease expiry is reaped lazily on the next call.
- No remote plane, wave scheduler service, host adapters, or dashboard.
- Background sessions are represented (`attachment`) but there is no process
  supervisor yet.
- `jj` workspaces, sandboxing, and shared dependency caches are not implemented.

Prev: [Local plane](./local-plane.md) · Next: [Remote plane](./remote-plane.md)
