# Agent integration (pi)

Intergent is driven by a **coordinator** session that spawns planner, worker,
and verifier subagents around a campaign DAG; the `intergent` engine owns
isolation (worktrees), plan-time directory ownership, verification, and
integration. pi is the supported agent harness. The workflow itself lives in
[`SKILL.md`](../SKILL.md) and its specification in
[Orchestration](./orchestration.md).

```
┌──────────────────────────┐        ┌──────────────────────────────┐
│  coordinator session     │        │  Intergent local plane       │
│  (pi)                    │──CLI──►│  service (single state owner)│
│                          │        │  worktrees · waves · DB      │
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
the package; there is no separate skill install. Both tools register inactive,
so a plain session never lists Intergent; `/skill:intergent <DESIGN.md>`
activates them for the session (the path must be an existing design document).

## Agent roles

| Role | Bound to a unit? | Contract |
|---|---|---|
| **Coordinator** | no | owns the plan (`dag.json`), spawns agents, calls `integrate` after a pass, writes the report |
| **Planner** | no | reads the design, writes `dag.json` |
| **Worker** | yes (its worktree) | edit owned directories → acceptance (CPU) → `commit` |
| **Verifier** | no (read-only) | runs acceptance (T0 then `tools/gpu.sh`), returns a verdict, never edits |

### Worker contract

1. `intergent status --short` must succeed — you are in your unit worktree.
2. Edit only files under the directories your DAG node owns (given to you as
   `dir:` scopes). `commit` enforces this: a changed path outside the owned
   directories is rejected, and the coordinator widens `owns` or adds a
   `depends_on` edge in `dag.json`.
3. Run the node's acceptance commands (CPU only; never the GPU).
4. `commit` and stop. Workers never run `integrate` or `git merge`.

### Coordinator

Start the coordinator with `/skill:intergent <DESIGN.md>` in pi, then use the
`campaign` tool — or drive the CLI directly: `start --no-unit` (which adopts the
current branch as the feature branch; check out your branch first), then
`spawn`/`verify`/`integrate` per ready node, a final idempotent `integrate`
sweep, and `report`. See [Orchestration](./orchestration.md).

## Process supervision

Campaign orchestration spawns one-shot subagents as **child processes of the
coordinator**; they are not detached and are not steerable. A coordinator crash
kills them, and a resumed campaign resets any `running` node to `pending`.

Prev: [Local implementation](./implementation.md) · Next: [Orchestration](./orchestration.md)
