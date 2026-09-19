# Conflict engine

The shared, deterministic core used by both planes. This is the part that must
be reproducible and explainable; an LLM must never sit in the verdict path.

## 1. Conflict taxonomy

| Class | Detector | Authority |
|---|---|---|
| Textual | `git merge-tree --write-tree` | Git |
| Symbol direct/dependency | AST (tree-sitter) defines/refs | Static analysis |
| Operation | Declared scope + operation tuple | Declaration |
| Intent/semantic | Declared scopes + rules (+ heuristic) | Declaration + rules |
| Behavioral | Build, tests, differential | Tests |

## 2. Scope canonicalization

Deterministic, language-aware normalization:

- case-fold; split CamelCase; strip namespace/path prefixes
  (`App\Services\Report::render` ≡ `Report::render`);
- path-clean file scopes; sort and de-duplicate;
- scope kinds: `dir, file, symbol, api, schema, config, migration, infra, test`.

## 3. Operation classes (load-bearing decision)

```
add | extend | modify              → preserves what others depend on
replace | remove | rename | migrate → does not
```

The operation is **declared by the agent**, not inferred from prose. Prose
inference is retained only for a human typing scopes without `=OPERATION`, is
marked `inferred: true`, and can never assert.

Rationale (measured in prior art): keyword inference caught 1 of 10 real
conflicts and raised false HIGH on 9 of 9 compatible pairs.

## 4. Matching tiers, assertion, severity

| Tier | Score | Can assert? |
|---|---|---|
| Exact canonical scope | 1.00 | **yes** |
| Same key, different kind | 0.90 | no |
| Token Jaccard ≥ 0.66 | 0.78–0.85 | no |

- **Asserted**: both sides *declared* operations on the *exact* same scope.
- **Surfaced**: anything inferred or loosely matched; **capped below HIGH**.

| Rule | Condition | Severity |
|---|---|---|
| `FM-C001 destructive_vs_additive` | one destructive, one additive, overlapping | HIGH if asserted, else MEDIUM |
| `FM-C002 divergent_rewrite` | both destructive | HIGH if asserted, else MEDIUM |
| `FM-C003 shared_contract` | both additive | MEDIUM |

Suggestions are scope-kind-aware: `schema`/`migration`/`config` → agree explicit
migration order; otherwise → extract a stable abstraction.

> **Destructive vs additive is not silently queued.** If one intent replaces
> what another extends, "waiting" then extending the old API is pointless. The
> engine surfaces a HIGH finding and requires an explicit decision — wait,
> redesign, or audited override.

## 5. Declaration vs diff reconciliation

If a declared operation disagrees with the AST diff (declared `extend` but the
symbol was removed), emit a finding. This catches confused or stale declarations
without an LLM.

## 6. The LLM's role (advisory only)

**Allowed**
- draft a declaration from task text + diff, for the agent to **confirm**;
- explain a finding and propose a resolution;
- triage/routing; summarize diffs for review;
- interpret CI/differential failures.

**Forbidden**
- computing the verdict or severity;
- blocking a candidate;
- being the sole basis for a HIGH finding.

Every LLM-derived finding is tagged `inferred`, capped below blocking severity,
and requires confirmation to become a declaration. Model + prompt versions are
pinned for provenance and caching. Deterministic checks stay on-box; LLM calls
are opt-in and explicit.

---

Prev: [Architecture](./architecture.md) · Next: [Local plane](./local-plane.md)
