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

Skills + MCP alone are insufficient: bootstrap (`start`) happens
before MCP exists; the daemon must be started and inspected; CI cannot speak
MCP; humans need status/override/audit without opening an agent; recovery from
broken MCP config needs a CLI; not every client supports MCP. Skills route and
fall back to the CLI; MCP exposes only the hot loop.

## Interfaces

### MCP tools (agent hot loop)

One tool, `ig`, parameterized by an `action` enum:

```
start   status   declare   commit   handoff
```

A single tool keeps the agent's context small; the enum (and every flag) is
generated from `intergent/surface.py`, so it can never drift from the CLI.
Human-only actions (`review --approve/--reject`) are never in the schema and are
rejected if called by name.

### CLI

```bash
intergent start | declare | commit | handoff | review | status | mcp
```

The local-plane reference implementation renders those six actions from
`intergent/surface.py`. Flags carry the long tail: `declare --dry-run`
(conflict check), `--renew`/`--release` (lease maintenance), `--decide`
(audited override); `commit --sync` (rebase); `status --health` (doctor),
`--simulate` (wave plan + combined-tree checks), `--gc` (worktree/branch cleanup),
`--short`, `--unit U`; `handoff`; `review --approve/--reject`. See
[Local implementation](./implementation.md).

### CI / host

Required check, webhooks, integration-branch bot, `merge_group`-style trigger.

---

Prev: [Overview](./overview.md) · Next: [Conflict engine](./conflict-engine.md)
