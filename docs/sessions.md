# Sliceme session suspend and resume design

Status: proposed.

This document describes how a pi agent session can be suspended when the user
asks and resumed later, and how the progress inside a long-running Sliceme
campaign is checkpointed so that resuming continues rather than restarts. It is
grounded in the current engine and adapter.

- Pi already stores every session as JSONL and reopens it with
  `pi --continue`, `pi --resume`, and `/resume` (`docs/sessions.md` in the pi
  distribution). The transcript needs no new storage.
- `sliceme/campaign.py` already prefixes DAG/state/report/log paths by the
  target branch, and `sliceme/coordinator.ts::startCampaign` already reconciles
  a campaign from git plus `state.db` on an existing `dag.json` (guide.md
  §Failure and resume).
- `sliceme/store.py` already recovers orphaned executor jobs
  (`recover_orphan_jobs`) and `sliceme/store.py` plus git win over `state.json`.
- `docs/observability.md` already proposes the durable `attempts` table and the
  per-node heartbeat files this design depends on for attempt fidelity.

The design adds an explicit checkpoint, a discovery registry, a cooperative
stop, and node-level continuation on top of that substrate.

## 1. Goals

- A user can suspend a pi session on request, at a safe point, without losing
  committed or uncommitted campaign work.
- A suspended session is discoverable, nameable, and resumable from the same
  checkout, from a second terminal, or after a process crash.
- A long Sliceme campaign resumes from the exact wave and node it stopped at:
  done nodes never re-run, committed candidates re-verify from cache, and
  a paused worker continues editing its preserved worktree.
- The user has a way to store and manage suspended sessions as campaigns.

### Non-goals

- Replacing pi's session picker. `/resume` stays pi's; this design adds campaign
  awareness on top of it.
- A daemon. Suspension is a file-and-SQLite checkpoint plus pi's own session
  file, consistent with "no long-lived daemon" (reference.md §6).
- Multiple concurrent campaigns per plane (still out of scope).
- Remote or cross-repository scheduling.

## 2. Two layers, one lifecycle

There are two durable things, and one lifecycle that parks and wakes both.

```text
                 /suspend                 pi --continue / /campaigns
   active ───────────────▶ checkpointing ─────────▶ suspended
     ▲   (pause flag +      (engine suspend)            │
     │    session_shutdown)                             │
     └───────────── resume (switchSession + auto-prompt)┘
```

- **Pi session layer.** The transcript is autosaved JSONL. Suspending means
  reaching a safe point, parking, and registering; resuming means reopening the
  transcript and injecting a continuation prompt.
- **Sliceme campaign layer.** The checkpoint records wave, node, attempt, and
  preserved worktrees. Resume reconciles from git plus `state.db`, the same way
  `start` already does.

The registry is the management surface. The per-campaign checkpoint is the
authoritative progress. Pi's JSONL remains the transcript.

## 3. Pi session layer

### 3.1 Commands

The Sliceme extension registers one entry point per user intent. Commands keep
the adapter thin and forward to the engine where state is involved.

| Command | Behavior |
|---|---|
| `/sliceme [DESIGN.md]` | Existing start entry point. Activates the tools and starts a campaign. |
| `/suspend [label]` | Park the current session: write the pause control flag, request a cooperative stop, checkpoint, and register. Works while the agent is busy (§5). |
| `/campaigns` | Interactive list of registered campaigns: label, branch, wave, done/total, last activity, status. Actions: resume, rename, show, prune, delete. |

`/resume` is deliberately not registered: pi owns it, and `session_start` with
reason `"resume"` provides the campaign hook. `ctx.hasUI` guards interactive
dialogs; JSON and print modes fall back to text and the CLI.

### 3.2 Event hooks

All three events already exist in the pi extension API.

- `session_shutdown` (reason `"quit" | "new" | "resume" | "fork"`) writes a
  checkpoint synchronously, so any exit, not only an explicit `/suspend`, is
  resumable. The handler must stay fast; use synchronous file writes and no
  subprocess calls.
- `session_start` (reason `"resume" | "startup"`) detects that the working
  directory is a suspended Sliceme plane with an open campaign for the current
  branch, and offers or auto-injects the resume prompt.
- `pi.appendEntry("sliceme.session", checkpoint)` records the checkpoint in the
  transcript so a reloaded session describes its own campaign state.

### 3.3 Storage and management

The registry is plane-authoritative with a rebuildable index, matching the
existing "everything is reconstructable from `.sliceme/` plus git" invariant.

| Store | Path | Role |
|---|---|---|
| Checkpoint | `.sliceme/<branch-key>.session.json` | Authoritative campaign progress and resume plan. |
| Queryable record | `campaign_sessions` table in `.sliceme/state.db` | Per-campaign row joined to the plane. |
| Global index | `${PI_AGENT_DIR:-~/.pi/agent}/sliceme/sessions.json` | Discovery cache for `/campaigns` and cross-repository resume. |
| Transcript | pi session JSONL | Conversation history, owned by pi. |

The global index points at checkpoints and can be rebuilt by scanning pi session
headers (`cwd`) against known `.sliceme/` planes (`sliceme sessions --rebuild`).
The `campaign_sessions` table is written through the additive migration path in
`Store._migrate`, like `jobs.timeout` and `candidates.node`.

```sql
CREATE TABLE IF NOT EXISTS campaign_sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pi_session_id TEXT,
  session_file TEXT,
  label TEXT,
  status TEXT NOT NULL DEFAULT 'active',   -- active | suspended | completed | failed
  reason TEXT,                             -- user | crash | budget | error
  wave INTEGER,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  suspended_at REAL
);
```

## 4. Sliceme campaign layer

### 4.1 New engine actions

Add these to `sliceme/surface.py` (the source of truth). `sliceme/cli.py` is
generated from the registry; `integrations/pi/unit.ts::SLICEME_ACTIONS` and
`tests/test_pi_package.py` follow.

```text
sliceme suspend [--reason user|crash|budget|error] [--grace SECONDS] [--drain]
sliceme resume  [--plan-only]
sliceme sessions [--json]
```

- `suspend` stops admitting new work, checkpoints the campaign, and records the
  reason. `--drain` finishes queued executor jobs instead of leaving them for
  recovery.
- `resume` reconciles from git plus `state.db`, recovers orphaned jobs, rebuilds
  waves, and returns the resume plan. `--plan-only` reports without mutating.
- `sessions` is the engine view of `campaign_sessions` for a second terminal.

### 4.2 Checkpoint file

`.sliceme/<branch-key>.session.json`:

```jsonc
{
  "campaign": "nanochat-cpp",
  "feature_branch": "feat/nanochat-cpp",
  "design": "DESIGN.md",
  "pi": { "session_id": "uuid", "session_file": "/home/u/.pi/agent/sessions/--repo--/....jsonl", "cwd": "/repo" },
  "status": "suspended",
  "reason": "user",
  "suspended_at": 1733234400.0,
  "current_wave": 1,
  "waves": [
    { "index": 0, "members": ["w1", "w2"], "cleanup_done": true }
  ],
  "nodes": {
    "w3": {
      "status": "paused",
      "attempt": 2,
      "unit": "w3-a2",
      "worktree": ".sliceme/worktrees/w3-a2",
      "branch": "sliceme/w3-a2",
      "candidate": 41,
      "last_tool": "edit",
      "heartbeat_age": 2.1
    }
  },
  "resume_plan": {
    "ready": ["w3"],
    "verify": ["w4"],
    "record_wave": 1,
    "blocked": []
  }
}
```

### 4.3 Node states and reconciliation

Extend the current `pending | running | done | failed | stopped` with `paused`.
On resume, reconcile from plane evidence, which always wins over the checkpoint:

| Plane evidence | Resume status |
|---|---|
| candidate `landed` | `done`; never re-run |
| candidate `prepared` | `pending`; verify first |
| `running` or `paused` with a preserved worktree | `paused`; continuation worker |
| `running` but the worktree is missing or unrecoverable | `pending`; fresh spawn |
| job `running` past its lease | requeued by `recover_orphan_jobs` |

### 4.4 Partial-work continuation

This is the main saving for long waves. On suspend, a `paused` worktree is never
reset. On resume, the coordinator spawns a continuation worker in the same
worktree with a prompt built from:

- the node goal and acceptance from `dag.json`;
- `git status --porcelain` and `git diff --stat` in the worktree;
- the last heartbeat (`last_tool`, a short argument, the last assistant text)
  from `.sliceme/<branch-key>.progress_<node>.json`;
- the recorded attempt number and the previous verifier evidence, if any.

A fresh spawn is the fallback when the worktree is gone or the diff cannot be
attributed.

### 4.5 Wave scope

Under wave scope (`exec --open/--record`) the shared `sliceme/wave-<N>` worktree
holds several nodes' edits. Suspend preserves it and records `record_wave` in
the resume plan. Resume first runs `exec --record --wave N` (conformance plus
per-node commits) before verifying, because the recorder holds the executor
lock and is serialized with check runs.

## 5. Cooperative suspension

A campaign is one long agent turn, so suspension must stop at a node or wave
boundary rather than mid-write.

1. `/suspend` writes `.sliceme/<branch-key>.control.json`:

   ```json
   { "pause": true, "requested_at": 1733234400.0, "label": "nanochat-cpp" }
   ```

   and sends a steering message (`pi.sendUserMessage(..., { deliverAs: "steer" })`)
   telling the coordinator to finish the current node and stop.

2. `coordinator.ts::spawnNode`, `ready`, and `ensureWaves` check the flag and
   return a `paused` result instead of spawning. Tool guidelines tell the model
   to stop on `paused`.

3. When the turn settles, the checkpoint runs (`suspend`), the registry is
   updated, and the user is told how to resume.

4. A hard abort (Escape, Ctrl+C) still works: `ctx.signal` propagates through
   `runSubagent` to a SIGTERM of the child, and `session_shutdown` writes the
   checkpoint. `SIGKILL` falls back to the existing recovery path.

A `--reason budget` suspend may be issued automatically when a wall-clock or
cost cap is hit, using the metrics from `docs/observability.md`.

## 6. Dependency: attempt fidelity

The quality of resume depends on the durable half of
`docs/observability.md` (its Phase 1): the `attempts` table and the per-node
heartbeat files
(`.sliceme/<branch-key>.progress_<node>.json`). They supply the timestamps,
`last_tool`, and per-attempt status the checkpoint and continuation prompt
consume. `integrations/pi/common.ts::runSubagent` already parses the
`pi --mode json` stream, so the accumulator and heartbeat writer are a small
addition. Build this substrate before, or together with, the checkpoint.

## 7. Interfaces touched

| Layer | Files |
|---|---|
| Engine | `sliceme/surface.py` (actions), `sliceme/service.py` (`suspend`, `resume`, `sessions`), `sliceme/store.py` (`campaign_sessions`, `attempts`), `sliceme/campaign.py` (checkpoint read/write and session paths), `sliceme/cli.py` (generated) |
| Adapter | `integrations/pi/coordinator.ts` (tool verbs, pause flag, event hooks, resume prompt), `integrations/pi/common.ts` (registry IO, heartbeat, checkpoint helpers), `integrations/pi/unit.ts` (`SLICEME_ACTIONS`) |
| Docs | this file; updates to `reference.md` (§1 actions, §3 state layout), `workflow.md`, `guide.md` (§Failure and resume), `database.md` (§11 related proposed work) |

## 8. Phased delivery

1. **Engine checkpoint primitive.** `suspend`/`resume` actions, the
   `campaign_sessions` table, the checkpoint file, and `.control.json`. No
   adapter change; testable from the CLI and reusing existing recovery.
2. **Attempt fidelity.** The `attempts` table, heartbeat files, and
   `attempt --begin`/`--end` wired into `spawnNode` and `verifyNode`, following
   `docs/observability.md`.
3. **Adapter hooks.** `session_shutdown` auto-checkpoint, `session_start` resume
   injection, the pause flag in `spawn`/`ready`, and the coordinator
   `suspend`/`resume` tool verbs.
4. **Management UX.** `/suspend` and `/campaigns`, `sliceme sessions`,
   `switchSession` resume, the global index, and `--rebuild`.
5. **Continuation and budgets.** Preserved paused worktrees, continuation
   workers, wave-scope record-on-resume, budget auto-suspend, and a prune
   policy for stale worktrees.

## 9. Test plan

- `tests/test_sessions.py` (new): checkpoint round-trip; `suspend` maps
  `running` to `paused`; `resume` maps `landed` to `done`, `prepared` to
  `pending`, and a preserved `paused` worktree to a continuation; orphan job
  recovery; idempotent suspend and resume; missing-worktree fallback; additive
  `campaign_sessions` migration on an older plane.
- Adapter tests: the pause flag blocks `spawn`; `session_shutdown` writes the
  checkpoint; `session_start` injects the resume prompt; registry list, rename,
  prune, and delete.
- `tests/test_pi_package.py`: the new actions appear in `surface.ACTIONS` and in
  `SLICEME_ACTIONS`.
- End-to-end: suspend after wave 0 and resume to completion; `kill -9` mid-wave
  and resume; assert no integrated node re-runs and no committed candidate is
  lost.

## 10. Open questions

- **Naming.** Prefer `/suspend` plus `/campaigns`; do not shadow pi's `/resume`.
- **Park versus exit.** Park and notify by default; auto-exit is opt-in. The
  base `ExtensionContext` exposes `ctx.shutdown()`; the command context does
  not, so a command parks and lets the user quit.
- **Registry authority.** Plane `state.db` authoritative with a global JSON
  cache (recommended) versus a global-only store.
- **Paused worktree retention.** Disk cost versus restart cost; add a
  time-boxed prune and surface it in `gc`.
- **Session-to-campaign cardinality.** One campaign per (plane, branch); a pi
  session may outlive a campaign, and a campaign may span sessions after a
  `/fork`.
