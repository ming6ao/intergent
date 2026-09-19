# Intergent — Documentation

> **Intergent** *(interlock + agent)* — coordination for parallel coding agents:
> no conflicts, local verification, mergeability-based landing.
>
> Status: design; the local plane is implemented as a dependency-free Python
> reference (see [Local implementation](./implementation.md)).

## Contents

| Doc | Covers |
|---|---|
| [Overview](./overview.md) | Summary, name, problem, goals/non-goals, core concepts |
| [Architecture](./architecture.md) | Planes, adapters, diagrams, interfaces |
| [Conflict engine](./conflict-engine.md) | Scopes, operations, rules, severity, LLM boundary |
| [Local plane](./local-plane.md) | Workspaces, sessions/workers, scope lock queue, verification |
| [Local implementation](./implementation.md) | Reference implementation: CLI/MCP, semantics, data model, tests |
| [Agent integration](./agents.md) | Run Intergent inside Claude Code, pi, or any CLI/MCP agent |
| [Remote plane](./remote-plane.md) | Conflict graph, wave scheduler, CI batching, integration modes |
| [Review & submission](./review-workflow.md) | Review packet, gates, pre-approval, workflows |
| [Operations](./operations.md) | Data model, tech stack, security and trust |
| [Roadmap & risks](./roadmap.md) | Phases and risk mitigations |
| [Prior art & open questions](./prior-art.md) | How this differs; unresolved decisions |

## Diagram

- [Architecture (interactive HTML)](./diagrams/architecture.html)
- Source spec: [`diagrams/architecture.json`](./diagrams/architecture.json)

## Naming

```
CLI:     intergent  (short alias: ig)
Daemon:  intergentd
```
