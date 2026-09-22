# Remote plane

Shared and authoritative. Owns scheduling, batching, and enforcement. It is the
only plane that can refuse a merge.

## 1. Candidate ingest

Candidates arrive via `intergent handoff`, PR, or webhook. Each carries: branch,
base, changed paths, symbol graph, declared intents, verification evidence,
priority.

## 2. Conflict graph

Nodes = pending candidates; an edge if any of:

- text conflict (`git merge-tree --write-tree`),
- symbol overlap (direct or dependency),
- declared-operation conflict on a shared scope.

## 3. Wave scheduler

```text
sort candidates by priority, then age, then change size
remaining = all
waves = []
while remaining:
    wave = []
    for c in remaining:                 # greedy maximal independent set
        if no edge between c and any member of wave:
            wave.append(c)
    if wave is empty: break             # cycle → tiebreak, force-rebase the loser
    waves.append(wave)
    remaining -= wave
```

- Within a wave, candidates are mutually mergeable → merge into one integration
  tree and run CI **once**.
- Conflicting candidates land in later waves; the later one is rebased onto the
  earlier (or surfaced as a required resolution).
- After a wave lands, recompute only edges touching changed paths/symbols
  (**incremental invalidation**).

## 4. Throughput and churn control

- **One CI run per wave**, results cached by tree hash.
- Content-addressed cache: `(base_tree, cand_tree) → mergeability`,
  `tree → test result`.
- **Batched pushes**, dedupe, exponential **backoff + jitter** (avoids thundering
  herd on rate limits).
- **Speculation**: build/test wave N+1 while wave N runs; discard on failure.
- Rebase policy is a knob: linear/semi-linear (rebase onto integration tip) vs
  merge commits.

## 5. Integration modes

| Mode | Mechanics | Use |
|---|---|---|
| **Host-mediated** | Intergent opens/updates PRs, drives order via required check + host merge queue | Teams; review/protection reused |
| **Tool-mediated** | Intergent owns an integration branch (bors-style), applies approved candidates in wave order, pushes, FF to `main` | Local/solo, or hosts without a merge queue |

---

Prev: [Local plane](./local-plane.md) · Next: [Review & submission](./review-workflow.md)
