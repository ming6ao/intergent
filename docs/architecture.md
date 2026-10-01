# Architecture

Intergent is **one engine, many adapters**: isolation, leases, verification, and
integration live in a single service layer; the CLI and the pi extension only
parse arguments and render results. No adapter owns state.

## Campaign topology

```text
COORDINATOR (top-level pi session)
 ├── PLANNER   reads the design, writes dag.json
 ├── WORKER_*  one-shot per ready DAG node
 │               unit worktree + scope leases
 │               declare -> edit -> acceptance (CPU) -> commit
 └── VERIFIER  read-only per candidate: T0 CPU then tools/gpu.sh
                    │
                    ▼
        integrate: --no-ff merge onto the feature branch
                   (fingerprint-cached combined checks)
```

The coordinator's own checkout is not a unit (`intergent start --no-unit`). Workers get
a `git worktree` + branch each. `ready(n) := every d in n.depends_on is done`,
where `done` means verified **and** integrated, so a dependent's base already
contains its dependencies' code. See [Orchestration](./orchestration.md).

## Modules

| Module | Responsibility |
|---|---|
| `intergent/surface.py` | **single source of truth**: action registry, validation, dispatch |
| `intergent/cli.py` | generated `argparse` CLI (`intergent`), human + `--json` output |
| `intergent/service.py` | **single owner of state**: sessions, units, intents, leases, candidates, integration |
| `intergent/store.py` | SQLite persistence (WAL) |
| `intergent/gitutil.py` | Git plumbing (worktree, merge, merge-tree, commit, branch) |
| `intergent/scopes.py` | Scope parsing, canonicalization, and the scope tree |
| `intergent/locks.py` | IS/IX/S/SIX/X compatibility matrix and requirement closure |
| `intergent/conflict.py` | Deterministic conflict rules `FM-C001..C003` and matching tiers |
| `intergent/verifier.py` | Fingerprints (plane and node sources) and trusted-check runner |
| `intergent/planner.py` | Greedy candidate wave packing + combined-tree simulation |
| `intergent/waves.py` | DAG wave projection for the scheduler (strict scope packing, `concurrency` cap) |
| `intergent/integrate.py` | Agent-callable feature-branch landing and node verification recording |
| `intergent/commitops.py` | Shared integration primitives (worktree, merge order, landed marking) |
| `intergent/campaign.py` | `dag.json` / `state.json` layout and readers |
| `intergent/report.py` | Deterministic campaign report skeleton |

State lives in the service + SQLite so it survives terminal sessions.

### Why the CLI still exists

Bootstrap (`start`) happens before any pi tool can act; CI runs the CLI; humans
need status and recovery; and the spawned tools call it as a subprocess. The
CLI is the engine surface, rendered from `intergent/surface.py`, and every other
adapter (the pi tools included) drives it or mirrors it. Because it is a bundled
script, no `intergent` install on `PATH` is required.

## Interfaces

### pi `campaign` tool (coordinator)

One tool, parameterized by an `action` enum, for the coordinator's orchestration
loop:

```
start   status   ready   spawn   verify   integrate   report
```

It calls the CLI internally (`common.ts` `runIg`) and adds DAG scheduling and
subagent spawning.

### pi `ig` tool (worker)

The unit lifecycle (`start`, `status`, `declare`, `commit`, `integrate`,
`report`) wrapped as one tool. `runSubagent` passes each agent's `tools:`
allowlist to `pi --tools`, so a worker gets `ig` and never `campaign`; the
verifier gets no Intergent tool at all. The CLI remains the single action
registry (`intergent/surface.py`), so no engine verb can exist in one adapter
and not another.

### CLI

```bash
intergent start | status | declare | commit | integrate | report
```

Flags carry the long tail: `start --no-unit` (adopts the current branch) and
`--main <branch>` (adopt an existing branch);
`declare --dry-run` (conflict check), `--renew`/`--release` (lease maintenance);
`integrate --node`, `--acceptance`, `--gpu`, `--check-only`, `--cleanup`;
`status --health`, `--simulate`, `--gc`, `--short`, `--unit U`; `report
--narrative`. See [Local implementation](./implementation.md).

---

Prev: [Overview](./overview.md) · Next: [Conflict engine](./conflict-engine.md)
