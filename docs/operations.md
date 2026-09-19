# Operations

## 1. Data model (sketch)

```text
agent(id, name, model, parent_id)
session(id, task, attachment)                  # foreground | tmux | background | systemd
unit(id, session_id, worktree, branch, kind)   # session | worker
intent(id, unit_id, task, summary, operation)
scope(id, kind, key, canonical)
intent_scope(intent_id, scope_id, operation, declared|inferred)
claim(id, unit_id, scope_id, mode, state, ttl, heartbeat_at)
candidate(id, unit_id, branch, base, priority, status)
fingerprint(candidate_id, tree, cmd_digest, toolchain_digest, policy_digest)
verification(candidate_id, fingerprint_id, status, output_ref)
edge(a, b, kind)          # text | symbol_direct | symbol_dep | operation
wave(id, base_tree, members, state)
decision(id, intent_id, related_intent_id, verdict, rationale, action)  # audited
```

Local store: **SQLite** (WAL). Remote store: **PostgreSQL**.

## 2. Tech stack

**Primary: Rust**

| Concern | Choice |
|---|---|
| Git (mutating) | shell out to system `git` (`worktree`, `merge`, `merge-tree`, `rebase`, `rerere`) |
| Git (read-only graph) | `gix` |
| Symbol graph | `tree-sitter` (+ language parsers); SCIP/LSPS later for precision |
| Local store | SQLite (`rusqlite`/`sqlx`, WAL) |
| Remote store | PostgreSQL (`sqlx`) |
| Local daemon API | unix socket; gRPC (`tonic`) / HTTP (`axum`) |
| CLI | `clap` |
| MCP | stdio server over the daemon API |
| Host integration | GitHub App (`octocrab`) + webhooks; GitLab REST |
| Optional engine | `jj-lib` for auto-rebase |
| Queue (remote) | Postgres `SELECT … FOR UPDATE SKIP LOCKED` or Redis |

**Alternative: Go** — `go-git` (read-only) + system git, tree-sitter Go bindings,
SQLite, gRPC, `cobra`, `go-github`, Postgres. Lower ceremony for the server;
slightly weaker for embedding `jj`.

Avoid Python/Node for the core (distribution + performance); expose the core
over MCP/gRPC if a Python-facing API is wanted.

> **Reference implementation.** [`intergent/`](../intergent) currently ships the
> local plane as a dependency-free Python 3.11+ package (stdlib only) so Phase 0
> behaviour is executable and testable now. It follows the same service/adapters
> split described in [Architecture](./architecture.md), so the local store,
> service API, and semantics can be ported to Rust/Go behind the CLI and MCP
> without changing the agent contract. See
> [Local implementation](./implementation.md).

## 3. Security and trust

- Agents run with the user's OS permissions; verification commands are trusted
  local code and are not sandboxed by default (optional sandbox later).
- LLM calls are opt-in and may ship code off-box; deterministic checks are local.
- Background sessions require an explicit autonomy policy (command allowlist,
  network on/off); they may **verify**, never **publish or land** unattended.
- All overrides and decisions are audited.
- Cross-machine identity/provenance is required at the remote plane; local
  provenance is not a remote identity signature.

---

Prev: [Review & submission](./review-workflow.md) · Next: [Roadmap & risks](./roadmap.md)
