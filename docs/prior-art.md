# Prior art and open questions

## 1. Prior art

- **jj** (Apache-2.0): auto-rebase + conflicts in commits — used as an optional
  local engine, not a scheduler.
- **Foremerge** (Apache-2.0): declared semantic scopes, advisory claims,
  verification gate — its deterministic, assumption-minimal stance informs the
  [conflict engine](./conflict-engine.md); it is single-machine and never lands
  code.
- **Mesh** (AGPL-3.0): worktrees, queue, file-level merge trains, AST conflicts —
  informs the [local](./local-plane.md) and [remote](./remote-plane.md) planes;
  its train packer is file-level and its multi-user tier is design-only.
- **Merge queues** (GitHub/GitLab, Zuul, bors-ng): serialize/batch landing but
  are not conflict-aware and expose no scheduler hook.

### What Intergent adds

A real conflict graph (text + symbol + declared operation), wave scheduling over
it, amortized merged-result CI, and a cross-user authoritative coordinator —
none of which the prior art combines. It also explicitly separates the
**authoring queue** (scope leases) from the **landing queue** (waves).

## 2. Open questions

1. Wave packing objective: maximize batch size, minimize expected CI cost, or
   minimize wall-clock — which is the default?
2. How aggressively should shared (S) scopes allow concurrency vs. serializing
   same-file edits?
3. Lease hold-until-ready vs hold-until-landed as the default?
4. Deterministic tiebreak for cyclic conflicts: priority, age, size, or
   reverse-dependency order?
5. How much of the symbol/dependency analysis can run at declare time (pre-code)
   vs. only after a diff exists?
6. Minimum viable cross-user identity model (OIDC? host identity? signed
   provenance?).

---

Prev: [Roadmap & risks](./roadmap.md) · [Back to index](./README.md)
