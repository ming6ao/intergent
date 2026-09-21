# Intergent

> *interlock + agent* — coordination for parallel coding agents: no conflicts,
> local verification, mergeability-based landing.

**Status:** design; local plane implemented as a reference implementation
(see [docs/implementation.md](./docs/implementation.md))

Intergent lets many coding agents work the same repository in parallel without
authoring conflicting changes, verifies their commits locally, and sequences
their landing on the shared branch by real mergeability while batching CI and
respecting git-host rate limits.

- **Local plane:** worktrees, declared intent, scope leases, fingerprint-pinned
  verification, combined-tree simulation.
- **Remote plane:** conflict graph, wave scheduler, merged-result CI, enforcement.

## Documentation

Start here: **[docs/README.md](./docs/README.md)**

- [Overview](./docs/overview.md) · [Architecture](./docs/architecture.md) ·
  [Conflict engine](./docs/conflict-engine.md) · [Local plane](./docs/local-plane.md) ·
  [**Local implementation**](./docs/implementation.md) ·
  [**Agent integration**](./docs/agents.md) ·
  [Remote plane](./docs/remote-plane.md) · [Review & submission](./docs/review-workflow.md) ·
  [Operations](./docs/operations.md) · [Roadmap & risks](./docs/roadmap.md) ·
  [Prior art & open questions](./docs/prior-art.md)
- **Architecture diagram:** [interactive HTML](./docs/diagrams/architecture.html)
  · [source spec](./docs/diagrams/architecture.json)

## Local plane quick start

The local plane is implemented in [`intergent/`](./intergent) (dependency-free
Python 3.11+). It gives every coding-agent session its own git worktree, leases
declared scopes so conflicting work is queued or escalated, verifies each
commit against a content fingerprint, simulates the combined tree, and merges
approved candidates into the local main branch in wave order.

```bash
# from a git repository: one idempotent bootstrap = plane + first unit
./bin/intergent start --check "tests=pytest -q"
# optional additional unit for another task
./bin/intergent start --name docs-agent --task "update API docs"
./bin/intergent declare --unit docs-agent --operation add --scope file:docs/api.md

# ... agent edits and commits in the printed worktree ...
./bin/intergent commit  --unit docs-agent -m "expand API docs"   # registers the candidate
./bin/intergent verify  docs-agent
./bin/intergent status  --simulate
./bin/intergent submit  docs-agent                               # approve + land (worktree removed)
```

Seven actions cover the whole lifecycle: `start`, `declare`, `commit`,
`verify`, `review`, `submit`, `status` (plus `mcp`). `declare` also does
`--dry-run` checks, `--renew`/`--release` leases, and `--decide` conflict
resolution; `commit --sync` rebases; `status --health/--simulate/--gc` covers
the old `debug` group. Agents get exactly **one** tool (MCP and pi) whose
`action` is one of these verbs. See [docs/implementation.md](./docs/implementation.md).

Agents may instead drive the same service over MCP: `./bin/intergent mcp`.
See [docs/implementation.md](./docs/implementation.md) for the full CLI/MCP
reference and the mapping from design concepts to code.

### Use it inside Claude Code or pi

Launch the agent **inside its unit worktree** and let it call Intergent (pi
bootstraps when a prompt tags Intergent; see [integrations/pi](./integrations/pi/README.md)):

```bash
./bin/intergent start --agent claude-code      # plane + a unit for this dir
cd "$(./bin/intergent --json start | python3 -c 'import sys,json;print(json.load(sys.stdin)["worktree"])')"
claude          # or: pi
```

- **Claude Code** (MCP): [integrations/claude/](./integrations/claude/README.md)
- **pi** (extension + skill, no MCP): [integrations/pi/](./integrations/pi/README.md)
- Bundled skill: [`SKILL.md`](./SKILL.md)
- Full guide: [docs/agents.md](./docs/agents.md)

Agents declare intent, commit, verify, and review; a human approves and lands
(with `review --land`, or the one-step `submit`). Landing is never exposed to
the agent.

### Install as an agent skill

```bash
npx skills add ming6ao/intergent -g -y -a pi -a opencode
```

This repository **is** an Agent Skill: the root [`SKILL.md`](./SKILL.md) bundles
the CLI (`bin/intergent` + the `intergent/` package), so no separate
`pip install` is required. See [docs/agents.md](./docs/agents.md).

```
CLI:     intergent  (alias: ig)
Daemon:  intergentd
```
