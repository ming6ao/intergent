# Sliceme observability design

Status: proposed.

This document describes how to make a running campaign observable, with the
primary goal of a **live, tqdm/rich-style progress display for the parallel
working subagents** (the workers a wave spawns). A second goal is a durable,
queryable projection so a second terminal or a finished report can show the
same numbers.

Scope of this revision: the earlier draft was engine-first and treated the
display as an afterthought. This revision puts the live multi-subagent display
first, defines the smallest path to it, and only then adds persistence. The
durable `attempts` table and per-node heartbeat files remain, because
`docs/sessions.md` and `docs/database.md` already depend on them.

It is grounded in the current engine and adapter:

- `sliceme/ownership.py` already projects the DAG into waves
  (`plan_dag_waves`, `DagWave`).
- `sliceme/store.py` already persists `units`, `candidates`,
  `fingerprints`, `verifications`, and `jobs`, including `jobs.duration`.
- `sliceme/executor.py` already exposes the single runner's queue
  (`Executor.status`, `Store.job_counts`).
- `integrations/pi/common.ts::runSubagent` already streams each subagent's
  full `pi --mode json` output to a per-node log and parses it line by line.
- `integrations/pi/coordinator.ts::widget` already renders wave lines, and
  multiple tool calls from one assistant message can run in parallel, so
  several `spawn` calls execute concurrently and block on their own
  `runSubagent`.

## 1. Goals and non-goals

### Goals

While a campaign runs, answer these questions live, from the coordinator
session and from a second terminal:

1. How many waves and nodes exist, how many are done, and which wave is current?
2. Which work units are running, pending, done, or failed?
3. For each running subagent: what is it doing right now, and for how long?
4. What are the aggregate and per-agent costs (turns, tool calls, tokens, cost)?
5. What are the timings (wall clock, queue wait, verification, lead time, ETA)?

### Non-goals

- A long-lived daemon or a second terminal renderer (reference.md §6). The
  live view is an in-process renderer plus small on-disk snapshots.
- Multiple concurrent campaigns per plane.
- Precise scientific billing. Token and cost figures are approximate rollups of
  the provider's cumulative usage.
- Replacing the pi TUI. The display is a `ctx.ui.setWidget` region and the
  per-tool `onUpdate` stream, nothing more.

## 2. Most important features first

The design is deliberately staged so a useful display exists before any new
database table or engine action. "Where" names the layer that owns the feature.

**Priority 0 — the live multi-subagent view (no persistence required).**

| # | Feature | Why it is first | Where |
|---|---|---|---|
| P0.1 | Normalized progress events reduced from the `pi --mode json` stream | One reducer feeds every renderer; without it there is no live data | `common.ts::runSubagent` |
| P0.2 | One in-process progress registry for all running subagents | A single owner prevents several workers from fighting over the widget | coordinator extension |
| P0.3 | One render timer (~4–10 Hz) that composes the widget | The only way to animate while `await runSubagent` is pending | coordinator extension |
| P0.4 | Per-subagent row: state, node, current tool + argument, elapsed | This is the tqdm/rich equivalent; it answers "what is it doing" | coordinator extension |
| P0.5 | Campaign aggregate line: done/total, wave k/n, elapsed, totals | The overall bar a user watches | coordinator extension |
| P0.6 | `spawn` streams its own row through `onUpdate` | Lets a second tool call or a narrow terminal see progress without the widget | coordinator extension |
| P0.7 | Width-safe, theme-aware string renderer | Widgets must fit; truncate with `truncateToWidth`, style with the theme | coordinator extension |

**Priority 1 — durable and queryable (crash-safe, second terminal).**

| # | Feature | Why next | Where |
|---|---|---|---|
| P1.1 | Debounced per-node heartbeat snapshot file | Survives a coordinator restart and feeds the engine projection cheaply | `common.ts` + `.sliceme/` |
| P1.2 | `attempts` table and `attempt --begin/--end` | Persists turns, tools, tokens, cost, and timings per subagent run | `store.py`, `surface.py`, `service.py` |
| P1.3 | `progress` action (`sliceme progress --json`) | Merges waves, state, SQLite, heartbeats, and the executor queue | `surface.py`, `service.py` |
| P1.4 | Stalled detection | A dead or pipe-blocked worker must not look "running" forever | `service.py` |
| P1.5 | `progress` added to `SLICEME_ACTIONS` | The surface parity test requires an exact match | `unit.ts` |

**Priority 2 — polish and campaign economics.**

| # | Feature | Why last | Where |
|---|---|---|---|
| P2.1 | ETA and throughput rates (tokens/min, tools/min) | Useful, but needs finished samples or event history | `service.py`, renderer |
| P2.2 | Wave durations, node lead time, critical-path estimate | Reporting value more than live value | `service.py` |
| P2.3 | Tool histogram, verification pass rate, cache hits | Aggregate insight | `progress`, renderer |
| P2.4 | Executor/verification section in the widget | There is only one runner, so it is one row, not many | coordinator extension |
| P2.5 | Metrics in the report skeleton | Records campaign cost after the fact | `campaign.py` |

## 3. What already exists

- **Wave projection is deterministic and complete.** `plan_dag_waves` produces
  `DagWave(index, members, conflicts)`, surfaced by `status` as `dag_waves` and
  reconciled into `state.waves` by `reconcileWaves`.
- **Some timing is already persisted.** `units.created_at`/`updated_at`,
  `candidates.created_at`/`updated_at`, `verifications.duration`, and the
  `jobs` table (`requested_at`, `started_at`, `finished_at`, `duration`,
  `exit_code`).
- **Executor queue state** is available from `Store.job_counts()` and
  `Executor.status()`.
- **Per-worker logs** contain the full `pi --mode json` event stream, including
  `tool_execution_start`/`tool_execution_end`, `turn_start`/`turn_end`,
  `message_end`/`message_update`, and cumulative `usage`.
- **Parallel tool calls are supported.** Pi can run several tool calls from one
  assistant message concurrently, so a wave's `spawn` calls already overlap.

## 4. Gaps

1. **No live updates.** `coordinator.ts::spawnNode` awaits `runSubagent`, and
   the widget is rendered only when `status` or `start` runs, so it is stale
   while any worker runs.
2. **No activity signal.** Nothing records the worker's current tool call, the
   currently running executor job, or the last assistant text.
3. **No agent statistics.** Turns, tool calls, tokens, and cost are parsed only
   to extract the final assistant text, then discarded.
4. **No aggregate rollup.** No single projection merges work-unit status, wave
   status, timings, executor state, and agent metrics.
5. **`state.json` has no timestamps** and is read-modify-written by each spawn,
   so parallel spawn completions can lose updates (see §11.2).
6. **No durability.** Live metrics exist only in the coordinator process, so a
   second terminal and a crashed session see nothing.

## 5. Design principles

- **The engine owns durable state; the adapter owns the live view.** Durable
  numbers come from SQLite and heartbeat files through the `progress` action.
  The animated view is presentation state derived from the same reducer.
- **One renderer, one registry.** Exactly one component in the coordinator
  process composes the widget. Subagents publish events; they never write the
  widget.
- **One event vocabulary.** Reduce the `pi` stream once into a small
  `ProgressEvent` set, and share that reducer between the live view and the
  durable writer. Do not parse the raw stream twice.
- **The renderer is pure.** `renderProgress(snapshot, options) -> string[]` has
  no I/O, so it is unit-testable and reusable for `onUpdate` and the second
  terminal.
- **Everything is reconstructable** from `.sliceme/` plus git after a crash.
- **Bound the cost.** Heartbeats are debounced, the render timer is single, and
  no per-event file write or per-frame subprocess is allowed.

## 6. Architecture for live multi-subagent progress

### 6.1 Normalized progress events

`runSubagent` already iterates the JSON lines. Extend `processLine` to map each
line into one small vocabulary. The `at` timestamp is the local receive time.

```jsonc
{"t":"attempt_started","node":"w1","attempt":1,"unit":"w1","agent":"worker","at":1733234401.0}
{"t":"turn","node":"w1","attempt":1,"turn":7,"at":1733234410.0}
{"t":"tool_started","node":"w1","attempt":1,"tool":"bash","args":"cargo test","at":1733234411.0}
{"t":"tool_finished","node":"w1","attempt":1,"tool":"bash","ok":true,"ms":812,"at":1733234411.8}
{"t":"usage","node":"w1","attempt":1,"input":45210,"output":3120,"cost":0.42,"at":1733234412.0}
{"t":"text","node":"w1","attempt":1,"text":"Running the CPU test suite...","at":1733234412.0}
{"t":"attempt_finished","node":"w1","attempt":1,"status":"ok","exit":0,"at":1733234463.2}
```

Mapping:

- `agent_start` → `attempt_started`.
- `turn_start` / `turn_end` → `turn` (count).
- `tool_execution_start` → `tool_started` (`toolName`, a short argument summary).
- `tool_execution_end` → `tool_finished` (success plus duration).
- `message_update.usage` → `usage` (cumulative per response; see §16).
- `message_end` (assistant) → `text` (authoritative last assistant text).
- `agent_settled` → `attempt_finished`.

### 6.2 In-process registry and the single renderer

A module-level `LiveProgress` in the coordinator process holds one record per
running attempt. It is the only writer of the widget. Every `runSubagent` call
publishes events to it; it publishes a snapshot on demand.

```text
runSubagent(w1) ─┐
runSubagent(w2) ─┼─▶ ProgressEvent ─▶ LiveProgress (one record per attempt)
runSubagent(w3) ─┘                          │
                                            ├─▶ LiveRenderer   (one timer, ~4–10 Hz)
                                            │     ├─ ctx.ui.setWidget("sliceme", lines)
                                            │     └─ onUpdate (each spawn's own call)
                                            │
                                            └─▶ HeartbeatWriter (debounced, ≤1 Hz)
                                                  ├─ .sliceme/<key>.progress_<node>.json
                                                  └─ attempts row (begin/end)
```

Because a render must be cheap and `runSliceme` spawns a Python process, the
live view cannot call the engine per frame. It renders from in-process state.
The engine `progress` action is for durability, a second terminal, and the
finished report; it reads the heartbeat files plus SQLite, never the live
registry.

### 6.3 Durable heartbeat files

Per running node, write `.sliceme/<branch-key>.progress_<node>.json` atomically
(`writeJson`), containing:

```json
{
  "node": "w1",
  "unit": "w1",
  "attempt": 1,
  "agent": "worker",
  "pid": 12345,
  "started_at": 1733234401.0,
  "updated_at": 1733234463.2,
  "turns": 7,
  "tool_calls": 23,
  "tools": {"edit": 8, "bash": 6, "read": 9},
  "last_tool": "bash",
  "last_tool_args": "cargo test",
  "last_text": "Running the CPU test suite...",
  "tokens_in": 45210,
  "tokens_out": 3120,
  "cost": 0.42
}
```

Per-node files avoid write races between parallel spawns and are cheap for the
engine to read. A heartbeat whose `updated_at` is older than
`max(5 s, 2 × poll interval)` is reported as `stalled` (§8.3).

### 6.4 Durable `attempts` table

Add to `sliceme/store.py`:

```sql
CREATE TABLE IF NOT EXISTS attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  node TEXT NOT NULL,
  unit TEXT,
  attempt INTEGER NOT NULL DEFAULT 1,
  agent TEXT NOT NULL DEFAULT 'worker',
  status TEXT NOT NULL DEFAULT 'running',
  started_at REAL NOT NULL,
  finished_at REAL,
  duration REAL,
  exit_code INTEGER,
  turns INTEGER NOT NULL DEFAULT 0,
  tool_calls INTEGER NOT NULL DEFAULT 0,
  tools TEXT,
  tokens_in INTEGER NOT NULL DEFAULT 0,
  tokens_out INTEGER NOT NULL DEFAULT 0,
  cost REAL NOT NULL DEFAULT 0,
  last_tool TEXT,
  last_activity_at REAL,
  error TEXT
);

CREATE INDEX IF NOT EXISTS idx_attempts_node ON attempts(node);
CREATE INDEX IF NOT EXISTS idx_attempts_status ON attempts(status);
```

An attempt is one subagent run for one node: a planner, a worker, or a verifier.
The table is added by the additive migration path in `Store._migrate`, so an
existing plane upgrades without a rebuild (matching `jobs.timeout` and
`candidates.node`).

Attempts are written through one engine action rather than from `state.json`,
so they survive a crash and are queryable from the CLI:

```text
sliceme attempt --begin --node w1 --attempt 1 [--unit w1] [--agent worker]
sliceme attempt --end   --node w1 --attempt 1 --status ok --exit 0 \
                        [--turns 7] [--tool-calls 23] [--tokens-in 45210] \
                        [--tokens-out 3120] [--cost 0.42] [--tools '{"bash":6}']
```

The surface parity test requires any new action to appear in
`integrations/pi/unit.ts::SLICEME_ACTIONS`.

### 6.5 The `progress` projection

Add to `sliceme/surface.py` (the source of truth) and implement in
`sliceme/service.py`; the CLI is generated from `surface.ACTIONS`:

```text
sliceme progress [--node ID] [--json]
```

It joins the DAG wave projection, `state.json`, the `attempts` table, existing
`units`/`candidates`/`verifications`/`jobs` rows, and the heartbeat files.
Output shape (abridged):

```json
{
  "campaign": {},
  "now": 1733234500.0,
  "totals": {
    "nodes": 12, "done": 4, "running": 2, "pending": 6, "failed": 0,
    "waves": 4, "current_wave": 1, "elapsed": 3600.0,
    "worker_seconds": 4210.0, "turns": 40, "tool_calls": 210,
    "tokens_in": 123456, "tokens_out": 7890, "cost": 1.23,
    "attempts": 6, "verifications": 5,
    "verification_pass_rate": 0.8, "queue_wait_seconds": 42.0
  },
  "waves": [
    {
      "index": 0, "status": "done", "members": ["w1", "w2"],
      "started_at": 1733234000.0, "finished_at": 1733234400.0,
      "duration": 400.0, "integrated": ["w1", "w2"]
    }
  ],
  "nodes": [
    {
      "id": "w1", "label": "human label", "wave": 0, "status": "running",
      "lead_time": 3600.0,
      "attempts": [
        {
          "attempt": 1, "unit": "w1", "status": "running",
          "started_at": 1733234401.0, "finished_at": null, "duration": 62.3,
          "last_tool": "bash", "last_tool_args": "cargo test",
          "turns": 7, "tool_calls": 23,
          "tokens_in": 45210, "tokens_out": 3120, "cost": 0.42,
          "last_activity_at": 1733234462.0, "heartbeat_age": 1.2, "stalled": false
        }
      ],
      "candidate": {}, "verification": {"status": "passed", "duration": 12.4},
      "job": {}
    }
  ],
  "executor": {
    "counts": {},
    "running_job": {
      "id": 7, "source": "node:w1", "wave": 0,
      "command": "cargo test", "elapsed": 88.0
    }
  }
}
```

## 7. Rendering rules (tqdm/rich-style)

`renderProgress` is a pure function of a snapshot and a frame counter. Plain
text lines are used because the widget accepts a string array, including in
remote-procedure-call mode. Styling uses the active pi theme.

```text
sliceme  ⣾ campaign  4/12 nodes · wave 1/4 · 01:00:00 · ETA ~00:22:30 · 131k tok · $1.23
  wave 0  ✓ done                    02:01
  wave 1  ● w1 edit src/api/foo.rs  01:02   7 turns  23 tools  45k tok
          ● w2 bash cargo test      00:48   4 turns  11 tools   8k tok
          · w3 queued                --:--
  wave 2  · w4 queued                --:--
executor  ⣽ node:w1 checks            00:01:30   queued 0
```

Rules:

- **Markers:** `✓` done, `●` running, `✗` failed, `⚠` stalled, `·` pending,
  `⣾⣽⣻⢿⡿⣟⣯⣷` spinner frames for running rows.
- **Aggregate bar:** a fixed-width progress bar plus `done/total`, current wave,
  elapsed, an approximate ETA, tokens, and cost. The bar is optional; the
  counters are mandatory.
- **Per-subagent row:** node, current tool with a shortened argument, then
  elapsed, turns, tools, tokens. Show `last_text` only when it fits, or when the
  node is stalled.
- **Verification/executor row:** one line for the single runner: current job
  source, elapsed, and queued count.
- **Elapsed clock:** `mm:ss` under an hour, `hh:mm:ss` above.
- **Colors:** running = accent, done = success, failed = error, stalled =
  warning, pending = muted.
- **Width safety:** truncate with `truncateToWidth` and measure with
  `visibleWidth`; never assume a fixed terminal width or the number of columns
  a Unicode marker occupies.
- **ETA:** show `~` and only when at least two nodes have finished. Estimate
  `average finished attempt seconds × remaining nodes ÷ concurrency`,
  clamped to a sane minimum. Never show a bare number as if precise.
- **Throughput:** tokens per minute and tools per minute, from a short window
  when event history exists, otherwise cumulative divided by elapsed.
- **Refresh:** one timer at 4–10 Hz while any attempt is running; stop the timer
  when the registry is empty, and render once on the final frame so the last
  state is not lost.

## 8. Data model details

### 8.1 Timing definitions

- **Work-unit wall clock:** `attempt.started_at` to `finished_at`, or `now` for
  a running attempt.
- **Queue wait:** `job.started_at - job.requested_at`.
- **Verification time:** `verifications.duration`.
- **Node lead time:** the first attempt's start to when the candidate lands
  (`candidates.updated_at` when the status becomes `landed`).
- **Wave duration:** the first member attempt's start to the last member's
  landing.
- **Critical path:** the sum of the longest per-node lead time across waves, a
  simple deterministic estimate from the same data.

### 8.2 Token and cost accounting

`message_update.usage` is cumulative for one assistant response. Sum the latest
value at each `message_end`; never add every delta. Record `tokens_in`,
`tokens_out`, and `cost` on the attempt row at `attempt --end`, and mirror them
into the heartbeat for the live view.

### 8.3 Stalled detection

A heartbeat is stalled when `now - updated_at > max(5 s, 2 × poll interval)`.
The node still reports `running`, but the display shows `⚠ stalled` and the age
of the last event. Stalled is a display state; it does not change `state.json`
and does not fail a node by itself.

### 8.4 What each subagent is doing

Resolution order:

1. An executor job is running for that node or wave: show the check command and
   elapsed time.
2. A fresh heartbeat exists: show `last_tool` with a short argument, the last
   assistant text, and the age of the last event.
3. No heartbeat and the node reports `running`: show `stalled` with the age.
4. Otherwise: show the status and the last verdict.

## 9. Metric catalog

| Group | Metric | Source |
|---|---|---|
| Wave | total waves, current wave, per-wave status and duration | `plan_dag_waves` + `state.json` + `attempts` |
| Work unit | status, wave, attempts, wall clock, lead time | `state.json`, `attempts`, `units` |
| Executor | queued/running counts, queue wait, check duration, cache hits | `jobs`, `verifications` |
| Verification | pass count, pass rate, duration | `verifications` |
| Agent | turns, tool calls, tool histogram, tokens in/out, cost, last tool | `attempts`, heartbeats |
| Campaign | elapsed, total worker seconds, total tokens, total cost, ETA | rollup of the above |

## 10. Interface changes

### Engine

- `store.py`: add the `attempts` table and migration, plus `create_attempt`,
  `finish_attempt`, `get_attempt`, `list_attempts`, and a job timing summary.
- `service.py`: add `progress()` (joining waves, `state.json`, SQLite rows,
  executor queue, and heartbeat files) plus `begin_attempt` and `end_attempt`.
- `surface.py`: add the `progress` and `attempt` actions. `cli.py` is generated
  from `surface.ACTIONS` and needs no manual change.

### pi adapter

- `unit.ts`: add `progress` and `attempt` to `SLICEME_ACTIONS`; the surface
  parity test requires an exact match.
- `common.ts`:
  - reduce the JSON stream into `ProgressEvent`s inside `runSubagent`;
  - accept an `onProgress` callback and a heartbeat path;
  - debounce heartbeat writes to at most one per second.
- `coordinator.ts`:
  - add the single `LiveProgress` registry and the render timer;
  - call `attempt --begin` before `runSubagent` and `attempt --end` after, for
    both `spawnNode` and `verifyNode`;
  - make `widget` render `renderProgress` instead of the current per-wave lines;
  - stream each spawn's row through the tool `onUpdate` callback;
  - include wave count, current wave, per-node elapsed, and totals in
    `summarise`;
  - add `progress` to the coordinator action list so the model can request a
    snapshot between spawns.

## 11. Architectural changes that make observability easy

These are suggestions, ordered by impact. P0 items do not depend on them; they
remove the sharp edges a live display would otherwise hit.

1. **Introduce one shared progress registry per coordinator process.** Today
   each `spawnNode` closure owns a `state` object and each renders the widget on
   demand. A single registry with one render timer is the difference between a
   smooth display and three workers overwriting each other. This is the single
   most important change.

2. **Centralize campaign-state mutation.** `spawnNode` reads
   `.sliceme/<key>.state.json`, mutates one node, and writes the whole file back
   when `runSubagent` resolves. Parallel spawns therefore race: the second
   writer can drop the first node's new status. Replace the read-modify-write
   with one in-process `CampaignStateStore` guarded by an async mutex, flushing
   atomically. This also gives the renderer a consistent snapshot.

3. **Normalize the subagent event stream once.** Parse `pi --mode json` in one
   place into `ProgressEvent`s, then reduce. Both the live view and the durable
   writer consume the reducer instead of re-parsing raw events. This avoids two
   subtly different notion of "turns" or "tokens".

4. **Separate the live sink from the durable sink behind one interface.** A
   `ProgressSink` with `event()` and `flush()` has an in-memory implementation
   for the widget and a file/`attempts` implementation for durability. The
   adapter never computes durable metrics; the engine `progress` action is the
   only durable formatter.

5. **Make the engine compute, the adapter only format.** Define `progress` as
   the stable schema and implement `renderProgress` as a pure formatter over a
   subset of it. Then the widget, `onUpdate`, the second terminal, and the
   report can share one renderer, and the renderer is unit-testable without a
   terminal.

6. **Keep spawn blocking for v1; revisit only if it hurts.** Pi runs tool calls
   from one message in parallel, so several `spawnNode` calls already overlap
   and a timer can animate during them. A background-spawn design (return a
   handle immediately, collect later) would decouple progress from the tool call
   but adds lifecycle work and weakens the "a coordinator crash kills the
   workers" invariant. Treat it as a decision gate, not a prerequisite.

7. **Prefer an atomic snapshot per node over a shared progress file.** Parallel
   writers must not append to one file. One `progress_<node>.json` written
   atomically, optionally paired with an append-only per-node event log for
   rate/ETA history, keeps reads cheap and races impossible.

8. **Do not add a watcher daemon.** A long-lived `sliceme progress --follow`
   process would simplify a second terminal but violates the no-daemon rule
   (reference.md §6). A second terminal polls `sliceme progress` instead.

## 12. Phased delivery

**Phase 0 — live in-process display (P0).**
Add the normalized reducer and `onProgress` to `runSubagent`; add the single
`LiveProgress` registry and render timer to `coordinator.ts`; replace `widget`
with `renderProgress`; stream `onUpdate` from `spawn`. This delivers the
tqdm/rich-style multi-subagent view with no schema or database change.

**Phase 1 — durable heartbeats and attempts (P1).**
Write debounced heartbeat files; add the `attempts` table, the `attempt`
action, and wire `--begin`/`--end` into `spawnNode` and `verifyNode`. Add the
`progress` action and `SLICEME_ACTIONS` entries. Add stalled detection.

**Phase 2 — campaign economics and the second terminal (P2).**
ETA, throughput, the tool histogram, wave/lead timings, verification pass rate,
and cache hits; the executor section in the widget; the metric catalog in the
report skeleton.

**Phase 3 — documentation.**
Update `docs/reference.md` (new actions and table), `docs/guide.md`, and
`docs/workflow.md`.

## 13. Test plan

- `tests/test_progress.py`: build a plane, create units, candidates, jobs,
  attempts, and heartbeat files with known timestamps; assert durations, queue
  wait, wave rollups, totals, heartbeat merge, and stalled detection.
- A unit test for `renderProgress` with fixed snapshots and widths: markers,
  truncation of a long tool argument, ETA suppression below two finished nodes,
  and stalled rows.
- Extend `tests/test_cli.py` and `tests/test_pi_package.py`: the new actions
  appear in `surface.ACTIONS` and in `SLICEME_ACTIONS`; the `attempts` table
  migrates additively on an older plane.
- A structural test that `runSubagent` still preserves the final assistant text
  after the reducer change.
- `tests/test_executor.py`: unchanged behavior, plus a timing assertion that
  `duration` is recorded.

## 14. Risks and decisions

- **Widget updates during a pending tool call.** The render timer and the tool
  `onUpdate` callback are the only ways to observe a worker from the same
  session while `await runSubagent` is pending. Confirm with the installed pi
  version that a timer may call `ctx.ui.setWidget` while a tool call is
  outstanding; if not, `onUpdate` still covers the same session.
- **Log volume and backpressure.** Heartbeat writes must be debounced, and the
  JSON stream must keep being consumed (pi stalls when the pipe fills), which
  the current `processLine` loop already does. Do not let rendering slow the
  line reader.
- **Lost updates.** Without §11.2, parallel spawn completions can clobber
  `state.json`. Fix this before enabling many parallel spawns, not after.
- **Token accounting semantics.** `message_update.usage` is cumulative per
  assistant response, so sum the latest value per `message_end`, not every
  delta.
- **Metric sensitivity.** Token and cost data are written under the
  git-excluded `.sliceme/`, matching the existing report and job tables.
- **ETA honesty.** Agent work is not uniform; show ETA as a rough marker and
  suppress it until there are samples.

## 15. Related proposed work

`docs/sessions.md` builds on this design: the durable `attempts` table and the
per-node heartbeat files record how far the current worker got and let a resumed
campaign continue that attempt instead of restarting it. Implement Phase 1 here
before, or together with, the checkpoint primitive.
