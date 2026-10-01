# Agent integration (pi)

Intergent is driven by a **coordinator** session that spawns planner, worker,
and verifier subagents around a campaign DAG; the `intergent` engine owns
isolation (worktrees), scope leases, verification, and integration. pi is the
supported agent harness. The workflow itself lives in [`SKILL.md`](../SKILL.md)
and its specification in [Orchestration](./orchestration.md).

```
┌──────────────────────────┐        ┌──────────────────────────────┐
│  coordinator session     │        │  Intergent local plane       │
│  (pi)                    │──CLI──►│  service (single state owner)│
│                          │        │  worktrees · leases · DB     │
└───────────┬──────────────┘        └──────────────┬───────────────┘
            │ spawns one-shot                       │ verify + integrate
            ▼                                       ▼
   planner · worker(s) · verifier            feature branch (per campaign)
```

## Install

The repository is a **pi package** that ships the `campaign` coordinator tool,
the `ig` worker tool, and the bundled skill:

```bash
pi install ./                                       # local checkout
# pi install git:github.com/ming6ao/intergent
# pi install npm:intergent
pi                                                  # launch the coordinator
```

See [`integrations/pi/`](../integrations/pi/README.md). The skill ships with
the package; there is no separate skill install.

## Agent roles

| Role | Bound to a unit? | Contract |
|---|---|---|
| **Coordinator** | no | owns the plan (`dag.json`), spawns agents, calls `integrate` after a pass, writes the report |
| **Planner** | no | reads the design, writes `dag.json` |
| **Worker** | yes (its worktree) | `declare` → edit → acceptance (CPU) → `commit` |
| **Verifier** | no (read-only) | runs acceptance (T0 then `tools/gpu.sh`), returns a verdict, never edits |

### Worker contract

1. `intergent status --short` must succeed — you are in your unit worktree.
2. `declare` before editing, scoping every file/symbol.
   - `granted` → edit.
   - `queued` → **exit immediately** and report the blocker; the coordinator
     serializes the node or re-plans. Never force a conflict.
   - `needs_decision` → stop; the coordinator re-plans.
3. Run the node's acceptance commands (CPU only; never the GPU).
4. `commit` and stop. Workers never run `integrate` or `git merge`.

### Coordinator

Use the `campaign` tool in pi, or the CLI: `start --no-unit --main <feature>
--base <base>`, then `spawn`/`verify`/`integrate` per ready node, a final
idempotent `integrate` sweep, and `report`. See
[Orchestration](./orchestration.md).

## Process supervision

Campaign orchestration spawns one-shot subagents as **child processes of the
coordinator**; they are not detached and are not steerable. A coordinator crash
kills them, and a resumed campaign resets any `running` node to `pending`.

Prev: [Local implementation](./implementation.md) · Next: [Orchestration](./orchestration.md)
