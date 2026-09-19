# Intergent — Design

> **Intergent** *(interlock + agent)* — coordination for parallel coding agents:
> no conflicts, local verification, mergeability-based landing.

The design has been split into focused documents under [`docs/`](./docs/README.md).

| Doc | Covers |
|---|---|
| [Overview](./docs/overview.md) | Summary, name, problem, goals, concepts |
| [Architecture](./docs/architecture.md) | Planes, adapters, [diagram](./docs/diagrams/architecture.html), interfaces |
| [Conflict engine](./docs/conflict-engine.md) | Scopes, operations, rules, severity, LLM boundary |
| [Local plane](./docs/local-plane.md) | Workspaces, sessions/workers, scope lock queue, verification |
| [Local implementation](./docs/implementation.md) | Reference implementation: CLI/MCP, semantics, data model, tests |
| [Agent integration](./docs/agents.md) | Use Intergent inside Claude Code, pi, or any CLI/MCP agent |
| [Remote plane](./docs/remote-plane.md) | Conflict graph, wave scheduler, CI batching, integration |
| [Review & submission](./docs/review-workflow.md) | Review packet, gates, pre-approval, workflows |
| [Operations](./docs/operations.md) | Data model, tech stack, security |
| [Roadmap & risks](./docs/roadmap.md) | Phases and mitigations |
| [Prior art & open questions](./docs/prior-art.md) | Differentiation, unresolved decisions |

Start at **[docs/README.md](./docs/README.md)**.

```
CLI:     intergent  (alias: ig)
Daemon:  intergentd
```
