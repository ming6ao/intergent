# Local plane

Runs per user, on the developer's machine. Advisory but fast; holds isolation,
plan-time ownership, and local verification.

## 1. Workspace isolation

- One `git worktree` + branch per unit, sharing one object store.
- The coordinator's checkout is **not** a unit (`intergent start --no-unit`).
- Gitignored deps (`node_modules`, `.venv`, `target/`) are the user's
  responsibility; a shared dependency cache is out of scope.

## 2. Units and ownership

```text
Campaign (feature branch + dag.json)
 ├─ Coordinator (no unit)
 ├─ Worker (own worktree)   owns directories   e.g. dir:src/api
 └─ Verifier (no unit)      read-only; pinned to a commit fingerprint
```

- Coordinate **write units**, not every LLM call: if it can commit, it owns
  directories; if it only reads, it is out of the graph.
- A node owns the **deepest directories** that contain the paths it touches.
  Two nodes that own overlapping directory subtrees are serialized into
  different waves; there is no runtime declare, lease, or queue.
- There is no intent/conflict engine at runtime. The only authored ordering is
  `dag.json` (`owns` + `depends_on`), and it is fully consumed by the wave
  projection before any worker starts.

## 3. Plan-time serialization

`intergent/waves.py` packs nodes into waves (see
[Conflict engine](./conflict-engine.md)):

- a node is at least `max(wave(dep) + 1)` for every dependency;
- the per-wave size is capped by `concurrency` (default 3);
- no two members of a wave own overlapping directory subtrees.

`status.dag_waves` exposes the projection; the orchestrator persists it in
`state.json` and replans when the DAG fingerprint changes.

### Conformance at commit time

Because there is no lease, the guarantee is enforced after the worker commits:

```text
changed = git diff --name-only <unit.base_commit> <head>
any changed path outside the node's owned directories  ->  reject the commit
```

A rejection means the planner under-declared. The coordinator widens `owns` or
adds a `depends_on` edge; the DAG fingerprint changes and the next
`status`/`ready`/`spawn` replans.

## 4. Verification

- Trusted checks (build, typecheck, tests, lint) run in a clean worktree at the
  candidate commit.
- Result pinned to a **fingerprint** `(tree, cmd vector, toolchain, policy,
  source)`; any change invalidates it. `source` distinguishes the plane's check
  vector from a node's acceptance commands (`node:<id>`), so the two cannot
  collide.
- Agent-reported tests are provenance only, never acceptance.
- Only the verifier may use the GPU (`tools/gpu.sh`); workers stay on CPU.

## 5. Wave planning and simulation

`status --simulate` groups the prepared candidates by DAG wave, merges each
wave's candidates into a scratch tree, and runs the combined checks. The same
DAG-wave order drives `integrate`.

```text
intergent status --simulate
  wave 0: docs-agent, auth-agent
  wave 1: payments-agent        (depends_on auth-agent)
  wave 2: pay-agent             (depends_on payments-agent)
  combined checks: PASS
```

See [Orchestration](./orchestration.md) §3/§5.

---

## 6. Reference implementation

This plane is implemented under [`intergent/`](../intergent) with the CLI
`intergent`. The service layer (`intergent/service.py`) is the single
owner of state; the CLI and the pi extension are thin adapters, and SQLite (WAL)
is the local store. Key mappings:

| Design concept | Implementation |
|---|---|
| worktree + branch per unit | `start --name` → `ig/<unit>` branch and `.intergent/worktrees/...` |
| directory ownership + subtree conflict | `intergent/scopes.py` |
| wave projection | `intergent/waves.py` |
| commit plan conformance | `Service._conformance_violations` in `intergent/service.py` |
| fingerprint-pinned verification | `intergent/verifier.py` (`tree, cmd, toolchain, policy, source`) |
| candidate ordering + simulation | `intergent/planner.py` |
| campaign integration | `intergent/integrate.py` + `intergent/commitops.py` |
| `dag.json` / `state.json` | `intergent/campaign.py`; report in `intergent/report.py` |

See [Local implementation](./implementation.md) for the full command reference,
semantics, data model, tests, and deliberate gaps (no daemon/AST yet).

---

Prev: [Conflict engine](./conflict-engine.md) · Next: [Local implementation](./implementation.md)
