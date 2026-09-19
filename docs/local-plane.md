# Local plane

Runs per user, on the developer's machine. Advisory but fast; holds the
awareness, isolation, and local verification.

## 1. Workspace isolation

- Default: one `git worktree` + branch per unit, sharing one object store.
- Optional engine: `jj` workspaces (auto-rebase of descendants, conflicts stored
  in commits) behind the same interface.
- Gitignored deps (`node_modules`, `.venv`, `target/`) are handled with a shared
  dependency cache or per-worktree install.

## 2. Hierarchical session / worker model

```text
Session (tmux pane / task)        coarse intent + lease   e.g. feature:payments
 ├─ Worker (own worktree)         narrow intent + lease   symbol:PaymentService
 ├─ Worker (own worktree)         narrow intent + lease   file:docs/api.md
 └─ research/review subagents     not coordinated; verification pinned to a commit
```

- Coordinate **write units**, not every LLM call: if it can commit, it must
  declare; if it only reads, it is out of the graph.
- A parent declares a **coarse reservation**; children declare **narrow scopes**.
  Conflict detection runs at both levels.
- A worker that forks a worktree registers as a child; the daemon also
  auto-detects new worktrees/branches as a fallback.
- Verification subagents hold no lease; they are pinned to a commit fingerprint.
- Foreground and background sessions are the same object with different
  attachment (terminal, tmux, systemd, or built-in supervisor).

## 3. Scope lock manager and authoring queue

**Conflict prevention**, distinct from the landing queue.

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
scope order** → deadlock-free. Multi-scope requests are all-or-nothing with
timeout + backoff.

### Policy by operation class

| Case | Mode | Behavior |
|---|---|---|
| additive, disjoint symbols in same file | S | both proceed; co-test |
| additive, same symbol | S | both proceed; advisory; co-test |
| destructive (`replace`/`remove`/…) | X | **queue**: second waits |
| destructive vs additive | — | **do not silently queue**: require a decision (wait / redesign / override) |

### Lease lifecycle

```text
declare → conflict? ──no──► GRANTED ──heartbeat──► ready/verified ──► submit ──► RELEASE
                    │           └──── heartbeat lost (TTL) ─────────────────────────┘
                   yes
                    ▼
                 QUEUED(position, blocker, eta) ──on release/expiry──► GRANTED
```

- TTL + heartbeat so a crashed/idle unit cannot stall the queue.
- Fairness: priority + FIFO + **aging** (no starvation).
- Waiting semantics: return `queued{position, blocker, eta}`; the agent may poll,
  wait, **switch to non-conflicting work**, or proceed optimistically and rebase.
- **Lease holds until the candidate is ready (verified + submitted)**, then the
  waiter is released and rebases onto the holder's branch — keeps authoring
  throughput up without coupling to review latency.

## 4. Local verification

- Configured trusted checks (build, typecheck, tests, lint) run in a clean
  worktree at the candidate commit.
- Result pinned to a **fingerprint**; any change invalidates it.
- Agent-reported tests are provenance only, never acceptance.
- Commands run with the user's OS permissions; optional sandboxing is a later
  feature.

## 5. Local integration simulation

Before submission, merge **all of the user's ready candidates** into a scratch
branch and verify the combined tree. This is the only way to catch cross-agent
semantic breakage locally. Output includes the local wave plan and blockers.

```text
intergent status --simulate
  wave 1: docs-agent, auth-agent
  wave 2: payments-agent        (conflicts with wave 1 on symbol:PaymentService)
  wave 3: pay-agent             (depends on payments-agent; will rebase)
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
| worktree + branch per unit | `start --name` → `ig/<session>/<unit>` branch and `.intergent/worktrees/...` |
| declared intent | `declare --operation ... --scope ...` (`intergent/scopes.py`) |
| scope lock manager + queue | `intergent/locks.py` (IS/IX/S/SIX/X) and `lock_requests`/`claims` |
| fingerprint-pinned verification | `intergent/verifier.py` (`tree, cmd, toolchain, policy`) |
| local integration simulation | `intergent/planner.py` (`simulate`) |
| approval-gated landing | `intergent/landing.py` (`review --approve` → `review --land` / `submit`) |

See [Local implementation](./implementation.md) for the full command reference,
semantics, data model, tests, and the deliberate gaps (no daemon/AST yet).

---

Prev: [Conflict engine](./conflict-engine.md) · Next: [Remote plane](./remote-plane.md)
