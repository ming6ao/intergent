# Sliceme database design

Status: current.

This document describes the local plane's SQLite database: where it lives, how
it is opened and evolved, every table and column, how rows are created and
consumed across the campaign lifecycle, and the invariants that keep it a
consistent audit trail.

Implementation: `sliceme/store.py`. Business rules live in `sliceme/service.py`,
`sliceme/integrate.py`, and `sliceme/executor.py`; this module owns persistence
only. The plane directory layout is described in `docs/reference.md` §3.

## 1. Scope

The database holds what git and the plane files cannot express quickly:

- the set of sessions, isolated work units, and candidate commits;
- content fingerprints and verification verdicts pinned to them;
- the single executor's check queue and timing.

It deliberately does **not** hold the plan. `dag.json` and `state.json` are
files under `.sliceme/`; the plan is authoritative there and is read by the
engine. `state.json` is a rebuildable cache, and git plus this database win on
conflict.

## 2. Location and connection

- **Path:** `.sliceme/state.db` in the plane root (`util.db_path`).
- **Engine:** SQLite in write-ahead logging (WAL) mode.
- **Open** (`Store.__init__`):
  - `sqlite3.connect(path, timeout=10.0)`;
  - `row_factory = sqlite3.Row`;
  - `PRAGMA journal_mode=WAL`;
  - `PRAGMA foreign_keys=ON`;
  - `PRAGMA busy_timeout=5000`;
  - `executescript(SCHEMA)`, then `_migrate()`, then `commit()`.

WAL lets many readers proceed while one writer holds the write lock, which
suits a coordinator, parallel subagents, and a second terminal all reading.
`busy_timeout` lets a writer wait rather than fail immediately when another
process holds the lock.

## 3. Schema evolution

Schema creation is idempotent: every statement uses `CREATE TABLE IF NOT
EXISTS` and `CREATE INDEX IF NOT EXISTS`. Opening an older plane adds only what
is missing.

Structural changes go through `Store._migrate`, which calls `_ensure_columns`
to add columns that predate the current schema:

- `jobs.timeout INTEGER NOT NULL DEFAULT 3600`
- `candidates.node TEXT`

The pattern is additive only. New columns must have a default or be nullable,
because existing rows are not rewritten. Destructive changes (renames, drops,
type changes) are deliberately avoided so a running or older plane keeps
working.

## 4. Entity relationships

```text
sessions 1 ──── * units 1 ──── * candidates 1 ──── * fingerprints
                                      │                     │
                                      └───────── * verifications

jobs  (standalone; no foreign-key edges)
```

- A **session** groups units.
- A **unit** is one writer: a git worktree plus a branch. Kinds are `worker`
  and `wave`.
- A **candidate** is a committed head a unit offers for landing.
- A **fingerprint** pins a candidate to a content identity.
- A **verification** is a verdict against a fingerprint.
- A **job** is a check vector queued for the single executor. It is
  intentionally decoupled: the fingerprint is stored as text and the source is
  `node:<id>` or `wave:<n>`, not a foreign-key edge.

All `*_at` columns are `REAL` epoch seconds from `util.now()` (`time.time()`).

## 5. Table reference

### 5.1 `sessions`

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PRIMARY KEY AUTOINCREMENT | |
| `name` | TEXT UNIQUE NOT NULL | defaults to the unit name |
| `task` | TEXT | free task description |
| `attachment` | TEXT NOT NULL DEFAULT `'terminal'` | only value used today |
| `created_at` | REAL NOT NULL | |

Created by `Store.create_session` through `Service.create_session`, and only
when the name does not already exist. Read by `Store.get_session` (id or name),
`Service.create_workspace`, and `Service.create_wave_workspace`.

### 5.2 `units`

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PRIMARY KEY AUTOINCREMENT | |
| `session_id` | INTEGER → `sessions(id)` | |
| `name` | TEXT NOT NULL | node id, `wave-<N>`, or `<node>-a<attempt>` |
| `kind` | TEXT NOT NULL DEFAULT `'worker'` | `worker` or `wave` |
| `worktree` | TEXT NOT NULL | absolute path under `.sliceme/worktrees/` |
| `branch` | TEXT NOT NULL | `sliceme/<name>` (or the campaign worktree branch) |
| `base_commit` | TEXT | fork point |
| `state` | TEXT NOT NULL DEFAULT `'working'` | `working`, then `landed` |
| `created_at` | REAL NOT NULL | |
| `updated_at` | REAL NOT NULL | |
| UNIQUE(session_id, name) | | |

Created by `Service.create_workspace` and `Service.create_campaign_workspace` (the
wave-scope alias) after `gitutil.add_worktree` succeeds; if the insert fails the
worktree is cleaned up.
Updated by `Store.set_unit_state` (via `integrate.mark_landed`/`_mark_delivered`,
which sets `landed`). Read by `list_units`, `get_unit`, `require_unit`,
`Service.current_unit` (worktree containment, else branch match),
`Service.status`, `campaign.build_skeleton`, and `Service.gc`.

`gc` prunes worktrees and branches for units in state `landed` or `closed`. Only
`landed` is written today; `closed` is reserved.

### 5.3 `candidates`

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PRIMARY KEY AUTOINCREMENT | |
| `unit_id` | INTEGER NOT NULL → `units(id)` | |
| `branch` | TEXT NOT NULL | the unit's branch |
| `head_commit` | TEXT NOT NULL | candidate commit, later the merge commit |
| `base_commit` | TEXT | |
| `priority` | INTEGER NOT NULL DEFAULT 0 | ordering hint |
| `status` | TEXT NOT NULL DEFAULT `'prepared'` | `prepared`, `pending`, `landed`, `failed`, `blocked` |
| `summary` | TEXT | review text |
| `node` | TEXT | DAG node id (migration) |
| `created_at` | REAL NOT NULL | |
| `updated_at` | REAL NOT NULL | |

Index: `idx_candidates_status`.

Created by `Service.finish` (node scope) and `Service._record_wave_commits`
(wave scope, one per node with `node` set). `Service.finish` reuses an existing
`prepared`, `failed`, or `blocked` row and rewrites `head_commit` and
`status='prepared'` rather than accumulating duplicates for one unit.

Updated by `integrate.mark_landed`, which sets `status='landed'` and the merge
commit.

Read by `Store.list_candidates` (joins `units` to add `unit_name` and
`worktree`), `Store.get_candidate` (by id, else by unit name or node), and the
integration ordering functions `_candidate_for_node`, `plan_waves`,
`ordered_candidates`, plus `Service.status` and `campaign.build_skeleton`.

Ordering for landing is: wave index, then higher `priority`, then
`created_at`, then `id`. This is what makes landing deterministic.

### 5.4 `fingerprints`

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PRIMARY KEY AUTOINCREMENT | |
| `candidate_id` | INTEGER NOT NULL → `candidates(id)` | |
| `fingerprint` | TEXT NOT NULL | sha256 over the identity tuple |
| `tree` | TEXT NOT NULL | git tree hash |
| `cmd_digest` | TEXT NOT NULL | hash of the command vector |
| `toolchain_digest` | TEXT NOT NULL | git and Python versions plus lockfile hashes |
| `policy_digest` | TEXT NOT NULL | policy block from `config.json` |
| `source` | TEXT NOT NULL DEFAULT `'plane'` | `plane`, `node:<id>`, or `wave:<n>` |
| `created_at` | REAL NOT NULL | |
| UNIQUE(candidate_id, fingerprint) | | |

Created by `Store.get_or_create_fingerprint`, called from
`integrate._verify_and_record_plane` and `integrate.record_node_verification`.
The unique pair makes creation idempotent: an existing identity returns its id.

Read by those same functions and by `Store.latest_verification_for_fingerprint`
and `Store.latest_verification`, which join it to attach the source and the
fingerprint text.

The `source` is part of the identity so unrelated verdicts cannot collide. A
sandbox profile and an executor-semantics version are folded into the
fingerprint at computation time, so tightening isolation or changing how checks
run invalidates a cached verdict.

### 5.5 `verifications`

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PRIMARY KEY AUTOINCREMENT | |
| `candidate_id` | INTEGER NOT NULL → `candidates(id)` | |
| `fingerprint_id` | INTEGER NOT NULL → `fingerprints(id)` | |
| `status` | TEXT NOT NULL | `passed`, `failed`, `error` |
| `output` | TEXT | formatted check output |
| `duration` | REAL | seconds |
| `commands` | TEXT | JSON command vector |
| `gpu` | TEXT | GPU tier used |
| `created_at` | REAL NOT NULL | |

Created by `Store.add_verification` after `run_checks`, from
`integrate._verify_and_record_plane` and `integrate.record_node_verification`.

Read by `Store.latest_verification_for_fingerprint`, the cache lookup that
skips re-running an unchanged command vector, and by
`Store.latest_verification`, the newest verdict for a candidate. The latter
feeds `Service.status`, `Service._project_unit`, and `campaign.build_skeleton`.

### 5.6 `jobs`

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PRIMARY KEY AUTOINCREMENT | |
| `wave` | INTEGER | campaign wave |
| `requester` | TEXT | verifier id |
| `source` | TEXT NOT NULL | `node:<id>` or `wave:<n>` |
| `commit_ref` | TEXT NOT NULL | commit the checks run at |
| `tree` | TEXT | git tree hash |
| `commands` | TEXT NOT NULL | JSON command vector |
| `sandbox` | TEXT | JSON sandbox profile |
| `sandbox_digest` | TEXT | folded into the fingerprint |
| `gpu` | TEXT NOT NULL DEFAULT `'none'` | `none`, `T1`, `T2` |
| `priority` | INTEGER NOT NULL DEFAULT 0 | higher runs first |
| `status` | TEXT NOT NULL DEFAULT `'queued'` | `queued`, `running`, `passed`, `failed`, `error`, `cancelled` |
| `fingerprint` | TEXT | text fingerprint, not a foreign key |
| `attempt` | INTEGER NOT NULL DEFAULT 0 | placeholder, not yet advanced |
| `timeout` | INTEGER NOT NULL DEFAULT 3600 | per-command timeout (migration) |
| `requested_at` | REAL NOT NULL | enqueue time |
| `started_at` | REAL | claim time |
| `finished_at` | REAL | completion time |
| `duration` | REAL | measured run time |
| `exit_code` | INTEGER | first non-zero command exit |
| `output` | TEXT | formatted check output |
| `error` | TEXT | failure text |
| `runner_pid` | INTEGER | process that claimed the job |

Indexes: `idx_jobs_status`, `idx_jobs_fingerprint`.

Created by `Executor.submit` through `Store.create_job` after resolving the
sandbox and computing the fingerprint. `Store.find_passed_job` short-circuits a
submit whose fingerprint already passed, so no new row is inserted on a cache
hit.

Updated by `Executor.run_job` (marks `running` with `started_at` and
`runner_pid`, then terminal with `finished_at`, `duration`, `exit_code`,
`output`, and `error`), by `Executor.cancel`, and by
`Store.recover_orphan_jobs`, which returns expired `running` rows to `queued`.

Claimed by `Store.claim_next_job`, which selects the highest-priority queued row
and marks it `running` inside one transaction, so two runners can never take the
same job.

Read by `Executor.status` (`job_counts`, queued, running, recent rows),
`Executor.wait`, and `Service.status`.

## 6. Lifecycle: how data is created and used

1. **Plane bootstrap.** `start` writes `.sliceme/config.json` and opens
   `Store`, which creates the schema. No domain rows are inserted.
2. **Session and unit.** A campaign `spawn` creates a worktree and branch, then
   inserts a `sessions` row (if needed) and a `units` row with
   `state='working'`.
3. **Commit.** The worker commits, then `Service.finish` runs the
   plan-conformance check and inserts a `candidates` row with
   `status='prepared'`. A re-spawn for the same node reuses and resets a prior
   candidate row.
4. **Verification.**
   - The executor path inserts `jobs` rows from `exec --submit` and updates
     them through `exec --run`; a passing fingerprint is served from cache.
   - The integrate path inserts `fingerprints` and `verifications` rows and
     reuses an existing verdict when the fingerprint is unchanged.
5. **Delivery.** When every wave is done and the user approves, `deliver` merges
the campaign worktree branch into the target with `git merge --no-ff`, then
`mark_landed`/`_mark_delivered` sets the candidates and their unit to `landed`.
A generic non-campaign plane falls back to ordering `prepared` candidates by wave
and priority and merging each branch.
6. **Dashboard and report.** `Service.status` reads units, candidates, waves,
   and job counts. `campaign.build_skeleton` reads units, candidates, and the
   latest verification per candidate for the report.
7. **Cleanup.** `Service.gc` reads `landed`/`closed` units and removes their
   worktrees and branches. Rows are never deleted; the table remains the audit
   trail.

## 7. Access summary

| Table | Writers | Readers |
|---|---|---|
| `sessions` | `Service.create_session` | `Service.create_workspace`, `create_campaign_workspace`, `get_session` |
| `units` | `Service.create_workspace`, `create_campaign_workspace`, `integrate.mark_landed`, `gc` (branch prune) | `Service.status`, `current_unit`, `unit_detail`, `gc`, `campaign.build_skeleton` |
| `candidates` | `Service.finish`, `Service._record_wave_commits`, `integrate.mark_landed` | `Service.status`, `integrate.ordered_candidates`, `campaign.build_skeleton` |
| `fingerprints` | `integrate._verify_and_record_plane`, `integrate.record_node_verification` | same, plus `latest_verification*` |
| `verifications` | `integrate._verify_and_record_plane`, `integrate.record_node_verification` | `Service.status`, `_project_unit`, `campaign.build_skeleton` |
| `jobs` | `Executor.submit/run_job/cancel`, `recover_orphan_jobs` | `Executor.status/wait`, `Service.status` |

## 8. Status and value domains

| Table | Column | Values |
|---|---|---|
| `units` | `state` | `working` (default), `landed`; `closed` reserved |
| `candidates` | `status` | `prepared` (default), `pending`, `landed`, `failed`, `blocked` |
| `verifications` | `status` | `passed`, `failed`, `error` |
| `jobs` | `status` | `queued` (default), `running`, `passed`, `failed`, `error`, `cancelled` |
| `jobs` | `gpu` | `none`, `T1`, `T2` |
| `fingerprints` | `source` | `plane`, `node:<id>`, `wave:<n>` |

## 9. Concurrency and integrity

- `Store.tx()` commits on success and rolls back on exception. Methods that
  write outside it rely on the caller to `self.store.conn.commit()`; the
  executor commits explicitly after recording results.
- `claim_next_job` and `recover_orphan_jobs` use `tx()`, so job state
  transitions are atomic.
- Foreign keys are enabled. There are no cascade rules, and rows are not
  deleted, so referential integrity is preserved by construction rather than by
  cleanup.
- WAL plus `busy_timeout` allows a coordinator, subagents, and a second
  terminal to read the same plane safely.
- The unique constraints that matter for correctness are
  `sessions.name`, `units(session_id, name)`, and
  `fingerprints(candidate_id, fingerprint)`. The last one is what makes
  verification idempotent.

## 10. What is not in the database

- `dag.json` (the authored plan) and `state.json` (per-node status cache) are
  files under `.sliceme/`. The coordinator owns writing them; Python reads
  them. `state.json` is rebuildable, and git plus the database win on conflict.
- `events.jsonl` is the extension's append-only audit log.
- Worker logs are files, one per node.
- The report is a Markdown file.

## 11. Related proposed work

`docs/observability.md` proposes one additional table, `attempts`, to persist
per-subagent timings and agent metrics (turns, tool calls, tokens, cost, last
tool, last activity), written through a new `attempt` action. It would follow
the same additive-migration approach used for `jobs.timeout` and
`candidates.node`.

`docs/sessions.md` proposes a second table, `campaign_sessions`, to record the
pi session bound to a campaign (session file, label, status, suspend reason, and
wave) so a suspended campaign can be listed and resumed. It depends on the
`attempts` table and heartbeat files for attempt fidelity and follows the same
additive-migration approach.