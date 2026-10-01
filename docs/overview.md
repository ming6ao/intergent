# Overview

> **Intergent** *(interlock + agent)* — coordination for parallel coding agents:
> one campaign DAG, a git worktree per worker, scope leases, and
> fingerprint-pinned integration onto a feature branch.

## 1. Summary

Multiple coding agents of one campaign working the same repository collide in
ways Git is blind to: they edit different files that depend on each other, or
plan contradictory changes. The failure modes are *authoring
conflicts* (wasted, contradictory work) and *integration conflicts* (stale-tip
breakage, CI churn).

Intergent isolates each worker in a worktree, records declared intent, prevents
conflicting scopes from being authored concurrently, verifies each candidate
against a content fingerprint, and integrates verified candidates one node at a
time onto a campaign **feature branch**. A coordinator turns a design document
into a machine-readable DAG (`dag.json`) and drives planner, worker, and verifier
subagents against it.

The semantic verdicts are **deterministic** and derived from *declared* intent.
LLMs draft declarations and the plan; they never decide whether something blocks.

## 2. Name

**Intergent** = *interlock* + *agent*. A railway interlocking physically prevents
conflicting routes from being set; Intergent does the same for concurrent agent
work.

```
CLI:     intergent
```

## 3. Problem statement

| Failure | Why Git doesn't catch it |
|---|---|
| Two agents edit different files that depend on the same symbol | Text merge has no symbol graph |
| One agent replaces a symbol while another extends it | Clean text merge; semantically contradictory |
| Both add the same function / enum value / route | Clean text merge; duplicate or ambiguous symbol |
| Schema/config/API change invalidates another agent's work | No shared interface versioning |
| A dependent starts before its dependency is integrated | No dependency-aware scheduling |

**Root cause:** Git compares *text*, not *intent*, and landing is per-branch
rather than ordered by the dependency graph.

## 4. Goals / non-goals

### Goals
- Several agents of one campaign run concurrently without authoring conflicts.
- Every candidate is verified at its exact commit, and the combined feature
  branch is verified before it is trusted.
- Nodes land in dependency order: `done` means verified **and** integrated, so a
  dependent's base already contains its dependencies' code.
- Only the verifier touches the GPU; workers stay on the T0 CPU loop.

### Non-goals
- Replacing Git. Git remains the source of truth (commits, branches, remotes).
- Building an agent runtime/model loop. The coordinator spawns the client's own
  headless mode; it does not run models.
- A code review UI, or automatic resolution of arbitrary text conflicts.
- A remote/shared scheduler or cross-user coordination.

## 5. Core concepts

| Term | Definition |
|---|---|
| **Campaign** | One feature branch plus a `dag.json` plan and executor `state.json`. |
| **Coordinator** | The top-level session that owns the plan and drives the campaign. |
| **Node** | One DAG unit of work with `owns`, `depends_on`, `acceptance`, `gpu`. |
| **Unit** | An isolated writer: a worktree + branch (`ig/<name>`). |
| **Scope** | Canonical, hierarchical unit of contention: `dir:`, `file:`, `symbol:`, `api:`, `schema:`, `config:`, `migration:`. |
| **Operation** | Declared intent on a scope: additive (`add`, `extend`, `modify`) or destructive (`replace`, `remove`, `rename`, `migrate`). |
| **Intent** | `{scopes[], operation, task, summary}` declared before editing. |
| **Claim / lease** | Time-bounded, heartbeat-renewed grant of a lock mode on a scope. |
| **Candidate** | A committed unit awaiting verification and integration. |
| **Wave** | An ordered batch of mutually mergeable candidates used to order integration. |
| **Fingerprint** | Content hash of (commit tree, command vector, toolchain, policy, source) that pins a verification result. |

---

Next: [Architecture](./architecture.md) · [Conflict engine](./conflict-engine.md)
