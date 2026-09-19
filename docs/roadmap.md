# Roadmap and risks

## 1. Roadmap

| Phase | Deliverable |
|---|---|
| **0** | Local MVP: worktree-per-unit, MCP/CLI, SQLite intent registry, file-overlap warnings, fingerprint-pinned local verification. **Implemented** as a Python reference (see [Local implementation](./implementation.md)); port to Rust/Go pending. |
| **1** | Symbol graph + `git merge-tree` mergeability + local integration simulation. |
| **2** | Remote coordinator: ingest, conflict graph, wave scheduler, one-CI-per-wave, content-addressed cache, backoff+jitter, required-check enforcement. |
| **3** | Declared-intent scopes for non-code + cross-user identity/auth/provenance. |
| **4** | Optional `jj` engine, dashboard, GitHub App / GitLab adapters, sandboxing. |

## 2. Risks and mitigations

| Risk | Mitigation |
|---|---|
| False HIGH stalls the fleet | assertion requires declared ops on exact scope; inferred capped below HIGH |
| Missed semantic conflict | layered detectors + combined-tree tests; tests are ground truth |
| Deadlock in scope locks | root-to-leaf canonical acquisition; all-or-nothing + timeout |
| Starvation in the queue | priority + FIFO + aging |
| Crashed/idle lease holder | TTL + heartbeat |
| Stale verification cached | fingerprint invalidation |
| CI churn | one run per wave; content-addressed cache; speculative batching |
| Rate-limit thrash | batched pushes; dedupe; exponential backoff + jitter |
| Silent autonomy | background = verify only; publish/land requires human |
| Worktree leaks | GC on session end/crash |

---

Prev: [Operations](./operations.md) · Next: [Prior art & open questions](./prior-art.md)
