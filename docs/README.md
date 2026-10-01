# Intergent — Documentation

> **Intergent** *(interlock + agent)* — coordination for parallel coding agents:
> one campaign DAG, a git worktree per worker, scope leases, and
> fingerprint-pinned integration onto a feature branch.

The local plane and campaign orchestration are implemented in
[`intergent/`](../intergent) as a dependency-free Python reference. A pi package
ships the native tools, the CLI, and the skill ([`SKILL.md`](../SKILL.md)).

## Contents

| Doc | Covers |
|---|---|
| [Overview](./overview.md) | Summary, problem, goals, core concepts |
| [Architecture](./architecture.md) | Modules, adapters, and the campaign topology |
| [Conflict engine](./conflict-engine.md) | Scopes, operations, rules, severity |
| [Local plane](./local-plane.md) | Workspaces, scope lock queue, verification |
| [Local implementation](./implementation.md) | CLI reference, semantics, data model, tests |
| [Agent integration](./agents.md) | Run Intergent inside pi |
| [Orchestration](./orchestration.md) | Coordinator, planner/worker/verifier subagents, `dag.json` |

## Naming

```
CLI:     intergent  (alias: ig)
```
