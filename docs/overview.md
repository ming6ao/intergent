# Overview

> **Intergent** *(interlock + agent)* — a coordination layer that lets many
> coding agents work in parallel without conflicting, verifies their commits
> locally, and sequences their landing on the shared branch by real
> mergeability while batching CI and respecting git-host rate limits.

## 1. Summary

Multiple coding agents (Claude Code, Codex, Cursor, custom) working the same
repository collide in ways Git is blind to: they edit different files that
depend on each other, or plan contradictory changes. The failure modes are
*authoring conflicts* (wasted, contradictory work) and *landing conflicts*
(stale-tip breakage, CI churn, rate-limit thrash).

Intergent is two planes over one shared analysis core:

- a **local plane** (per user, on the developer's machine) that isolates agents
  into worktrees, records declared intent, prevents conflicting scopes from
  being authored concurrently, verifies commits, and simulates the combined
  result;
- a **remote plane** (shared service) that ingests ready candidates, builds a
  mergeability conflict graph, schedules non-conflicting batches ("waves"),
  runs merged-result CI once per wave, and enforces order at the host.

The semantic verdicts are **deterministic** and derived from *declared* intent
plus static analysis. LLMs draft declarations, explain findings, and triage —
they never decide whether something blocks.

## 2. Name

**Intergent** = *interlock* + *agent*. A railway interlocking physically prevents
conflicting routes from being set; Intergent does the same for concurrent agent
work. "Inter-" also carries mutual exclusion; "-gent" carries agent.

```
CLI:     intergent  (short alias: ig)
Daemon:  intergentd
```

## 3. Problem statement

| Failure | Why Git doesn't catch it |
|---|---|
| Two agents edit different files that depend on the same symbol | Text merge has no symbol graph |
| One agent replaces a symbol while another extends it | Clean text merge; semantically contradictory |
| Both add the same function / enum value / route | Clean text merge; duplicate or ambiguous symbol |
| Schema/config/API change invalidates another agent's work | No shared interface versioning |
| Every merge invalidates every other PR's CI | No merge queue / no batching |
| N agents push N times and hit secondary rate limits | No batching or backoff |
| Cross-user collisions | No shared view of pending intent across machines |

**Root cause:** Git compares *text*, not *intent*, and landing is per-PR rather
than scheduled by mergeability.

## 4. Goals / non-goals

### Goals
- Multiple agents of one user run concurrently without authoring conflicts.
- Every agent's commits are built, tested, and verified locally against the
  exact commit, and the *combined* result is verified before submission.
- Ready commits are sequenced by **real mergeability** (text + symbol + intent),
  batched into waves, and landed with minimal CI runs and git API churn.
- Human stays in control: review, approve, override — all audited.
- Deterministic, explainable, reproducible conflict verdicts; no LLM in the
  verdict path.

### Non-goals
- Replacing Git. Git remains the source of truth (commits, branches, remotes).
- Building an agent runtime/model loop. Intergent supervises the client's own
  headless mode; it does not run models.
- Being a code review UI. Delegate to the host's PR review where one exists.
- Cross-machine *local* coordination via shared filesystems (explicitly unsafe).
- Automatic conflict resolution of arbitrary text conflicts.

## 5. Core concepts

| Term | Definition |
|---|---|
| **Unit** | A thing that can produce commits: a session or a worker with its own worktree/branch. |
| **Session** | Top-level task, typically one tmux pane / one agent process. May fan out to workers. |
| **Worker** | A child unit under a session that owns its own worktree/branch. |
| **Scope** | Canonical, hierarchical unit of contention: `dir:`, `file:`, `symbol:`, `api:`, `schema:`, `config:`, `migration:`. |
| **Operation** | Declared intent on a scope: additive (`add`, `extend`, `modify`) or destructive (`replace`, `remove`, `rename`, `migrate`). |
| **Intent** | `{scopes[], operation, task, summary}` declared before editing. |
| **Claim** | Advisory/leased ownership of a scope by a unit. |
| **Lease** | Time-bounded, heartbeat-renewed grant of a lock mode on a scope. |
| **Candidate** | A finished, verified unit awaiting landing (branch + metadata + evidence). |
| **Wave** | An ordered batch of mutually mergeable candidates that can be tested/landed together. |
| **Fingerprint** | Content hash of (candidate tree, command vector, toolchain, policy digest) that pins a verification result. |

---

Next: [Architecture](./architecture.md) · [Conflict engine](./conflict-engine.md)
