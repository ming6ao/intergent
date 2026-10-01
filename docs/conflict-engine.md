# Directory ownership (conflict scheduling)

> Status: **implemented**. Intergent decides serialization entirely at plan
> time. There is no runtime conflict engine, no operation taxonomy, and no
> lease queue.

## 1. What a node owns

Every campaign DAG node declares `owns`: a list of repo-relative
**directories**. A node must name the deepest directory that contains each path
it will add, modify, or delete.

```jsonc
{ "id": "w1", "owns": ["dir:src/api", "dir:src/api/v1"], "depends_on": [] }
```

Normalization:

| Input | Canonical |
|---|---|
| `dir:src/api` | `src/api` |
| `src/api/` | `src/api` |
| `dir:.`, ``, `/` | `.` (the repository root) |

Non-directory specs — `file:`, `symbol:`, `api:`, `schema:`, `config:`,
`migration:`, `infra:`, `test:` — are rejected when the DAG is projected. A plan
that tries to own a single file fails loudly rather than silently receiving
directory-level serialization.

## 2. The conflict rule

Ownership is a **subtree**. Two nodes conflict when one owned directory is

* equal to,
* an ancestor of, or
* a descendant of

the other's, compared on path-segment boundaries. The root `.` is an ancestor
of every directory, so a node that owns `dir:.` serializes against every other
node.

```
src/api       vs src/api        -> conflict (equal)
src           vs src/api        -> conflict (ancestor)
src/api       vs src/api/v1     -> conflict (descendant)
src/api       vs src/service    -> ok       (siblings)
src/models    vs src/model      -> ok       (no token similarity tier)
```

There is deliberately no fuzzy matching: the old token-Jaccard and
cross-kind tiers are gone, so a plan's concurrency is explainable from its
`owns` sets alone.

## 3. Where it is enforced

`intergent/waves.py` projects `owns` + `depends_on` into waves. A node is placed
in the earliest wave that

* is at least `max(wave(dep) + 1)` for every dependency,
* has room under the campaign's `concurrency` cap (default 3), and
* contains no node whose owned directories overlap.

`Service.status()` returns that projection as `dag_waves`; the coordinator
persists it in `state.json` keyed by a fingerprint of `(id, owns, depends_on)`,
so editing the DAG automatically replans.

## 4. Conformance: the runtime guarantee

Because there are no leases, the pre-edit guarantee is replaced by a post-commit
check. When a unit finishes:

```
changed = git diff --name-only <unit.base_commit> <head>
violations = [p for p in changed if p not in the subtree of any owned dir]
```

Any violation raises an error and the candidate is not registered. The
coordinator then widens the node's `owns` or adds a `depends_on` edge and
respwns. This keeps "no two same-wave units touch the same directory" auditable
without runtime locking.

## 5. Authoring guidance

* Own the **deepest** directory that contains the work; owning a parent
  serializes its whole subtree.
* Keep same-wave `owns` disjoint.
* Route shared build files (`BUILD`, `Cargo.toml`, lockfiles) to an explicit
  aggregation node that every touched component `depends_on`; that node owns the
  shared directory (`dir:.` for root files).
* Express ordering that same-directory serialization does not already give you
  with `depends_on`, never by hoping for a runtime queue.
