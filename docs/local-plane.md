# Local plane

Runs per user, on the developer's machine. Advisory but fast; holds isolation,
leases, and local verification.

## 1. Workspace isolation

- One `git worktree` + branch per unit, sharing one object store.
- The coordinator's checkout is **not** a unit (`ig start --no-unit`).
- Gitignored deps (`node_modules`, `.venv`, `target/`) are the user's
  responsibility; a shared dependency cache is out of scope.

## 2. Units and scopes

```text
Campaign (feature branch + dag.json)
 ├─ Coordinator (no unit)
 ├─ Worker (own worktree)   narrow intent + lease   e.g. file:src/tensor.cc
 └─ Verifier (no lease)     read-only; pinned to a commit fingerprint
```

- Coordinate **write units**, not every LLM call: if it can commit, it must
  declare; if it only reads, it is out of the graph.
- A worker declares **narrow scopes** before editing; the coordinator serializes
  or re-plans when two nodes would overlap.

## 3. Scope lock manager and authoring queue

**Conflict prevention**, distinct from integration order.

### Modes (multi-granularity locking over the scope tree)

```text
Locks: IS IX S SIX X
Compatibility (granted \ requested):
          IS   IX   S    SIX  X
    IS     ✓    ✓    ✓    ✓    ✗
    IX     ✓    ✓    ✗    ✗    ✗
    S      ✓    ✗    ✓    ✗    ✗
    SIX    ✓    ✗    ✗    ✗    ✗
    X      ✗    ✗    ✗    ✗    ✗
```

Rules: to take `S`/`IS` on a node, hold `IS`+ on its parent; to take
`X`/`IX`/`SIX`, hold `IX`+ on its parent. Acquire **root-to-leaf in canonical
scope order** → deadlock-free. Multi-scope requests are all-or-nothing.

### Policy by operation class

| Case | Mode | Behavior |
|---|---|---|
| additive, disjoint symbols in same file | S | both proceed; co-test |
| additive, same symbol | S | both proceed; advisory; co-test |
| destructive (`replace`/`remove`/…) | X | **queue**: second waits |
| destructive vs additive | — | **do not silently queue**: return `needs_decision`; the coordinator re-plans |

### Lease lifecycle

```text
declare → conflict? ──no──► GRANTED ──heartbeat──► commit ──► integrate ──► RELEASE
                    │           └──── heartbeat lost (TTL) ───────────────────────┘
                   yes
                    ▼
                 QUEUED(position, blocker) ──on release/expiry──► GRANTED
```

- TTL + heartbeat so a crashed/idle unit cannot stall the queue.
- Waiting semantics: a worker that receives `queued` **exits immediately** and
  reports the blocker; the coordinator serializes the node (adds a
  `depends_on` edge) or re-plans. A worker never blocks or overrides.

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

`status --simulate` merges the prepared candidates into scratch trees and runs
the combined checks. The same planner orders `integrate`.

```text
intergent status --simulate
  wave 1: docs-agent, auth-agent
  wave 2: payments-agent        (conflicts with wave 1 on symbol:PaymentService)
  wave 3: pay-agent             (depends on payments-agent)
  combined checks: PASS
```

---

## 6. Reference implementation

This plane is implemented under [`intergent/`](../intergent) with the CLI
`intergent` / `ig` and an MCP stdio server (`intergent mcp`). The service layer
(`intergent/service.py`) is the single owner of state; the CLI and MCP are thin
adapters, and SQLite (WAL) is the local store. Key mappings:

| Design concept | Implementation |
|---|---|
| worktree + branch per unit | `start --name` → `ig/<unit>` branch and `.intergent/worktrees/...` |
| declared intent | `declare --operation ... --scope ...` (`intergent/scopes.py`) |
| scope lock manager + queue | `intergent/locks.py` (IS/IX/S/SIX/X) and `lock_requests`/`claims` |
| fingerprint-pinned verification | `intergent/verifier.py` (`tree, cmd, toolchain, policy, source`) |
| wave planning + simulation | `intergent/planner.py` |
| campaign integration | `intergent/integrate.py` + `intergent/commitops.py` |
| `dag.json` / `state.json` | `intergent/campaign.py`; report in `intergent/report.py` |

See [Local implementation](./implementation.md) for the full command reference,
semantics, data model, tests, and deliberate gaps (no daemon/AST yet).

---

Prev: [Conflict engine](./conflict-engine.md) · Next: [Local implementation](./implementation.md)
