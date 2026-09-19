# Architecture

## Diagram

- **[Interactive architecture diagram (HTML)](./diagrams/architecture.html)**
- Source spec: [`diagrams/architecture.json`](./diagrams/architecture.json)

```text
        LOCAL PLANE (per user / machine)                        REMOTE PLANE (shared)
  agents ──MCP/CLI──► local coordinator (intergentd)      ┌─► coordinator service
                       ├ session/worker manager           │    ├ candidate ingest (webhook/API)
                       ├ intent + claim registry (SQLite) │    ├ conflict graph
                       ├ scope lock manager + queue  ◄────┼──► ├ wave scheduler
                       ├ local verifier (fingerprints)    │    ├ merged-result CI orchestrator
                       └ local integration simulator      │    └ integration branch + enforcement
                       │                                  │
              ┌────────┴─────────┐                        │
              │ shared analysis  │◄───────────────────────┘
              │ core (library)   │
              └──────────────────┘
                       │
              git host (GitHub/GitLab) + CI
```

## One engine, many adapters

MCP, CLI, CI, the host bot, and the dashboard all call the **same service API**
over the **same core**. No adapter owns state.

| Adapter | Audience | Scope |
|---|---|---|
| **MCP** (stdio) | agents | hot loop only, small tool set to limit context bloat |
| **CLI** | humans, CI, recovery | bootstrap, diagnostics, long tail, admin |
| **Session supervisor** | background workers | spawn/monitor/resume headless clients; also attaches to tmux/systemd |
| **Host bot** | git host | PRs, required check, integration branch |
| **Dashboard** | team | optional status board over the remote plane |

The MCP server is a **thin client of the daemon**, never the state owner. State
lives in the long-lived `intergentd` (local) and the coordinator service
(remote), so it survives terminal sessions.

### Why the CLI still exists

Skills + MCP alone are insufficient: bootstrap (`init`/`setup`/`doctor`) happens
before MCP exists; the daemon must be started and inspected; CI cannot speak
MCP; humans need status/override/audit without opening an agent; recovery from
broken MCP config needs a CLI; not every client supports MCP. Skills route and
fall back to the CLI; MCP exposes only the hot loop.

## Interfaces

### MCP tools (agent hot loop)

```
register_agent      create_workspace    register_child
declare_intent      check_conflicts     claim_scope
commit_workspace    verify              finish_workspace
status              submit
```

### CLI

```bash
intergent init | setup <client> | daemon | doctor
intergent agent start | status | review <candidate> | approve | reject
intergent simulate | submit | plan | land | override --reason
intergent session spawn --background [--tmux] | attach | list | resume | kill
```

The local-plane reference implementation currently provides
`init`, `doctor`, `agent register|list`, `session create|list`,
`workspace create|list|show`, `declare`, `check`, `heartbeat`, `release`,
`decide`, `override`, `rebase`, `commit`, `finish`, `verify`, `simulate`,
`review`, `approve`, `reject`, `land`, `status`, `gc`, and `mcp`. See
[Local implementation](./implementation.md).

### CI / host

Required check, webhooks, integration-branch bot, `merge_group`-style trigger.

---

Prev: [Overview](./overview.md) · Next: [Conflict engine](./conflict-engine.md)
