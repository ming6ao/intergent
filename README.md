# Intergent

> *interlock + agent* — coordination for parallel coding agents: one campaign
> DAG, a git worktree per worker, scope leases, and fingerprint-pinned
> integration onto a feature branch.

Intergent lets several coding agents work the same repository in parallel
without authoring conflicting changes, verifies each candidate against a
content fingerprint, and lands verified candidates one DAG node at a time on a
campaign feature branch. It is a dependency-free Python 3.11+ engine
([`intergent/`](./intergent)) plus a pi coordinator tool.

- **Isolation:** one `git worktree` + branch per worker unit.
- **Leases:** declared scopes are granted, queued, or escalated deterministically.
- **Verification:** plane checks and per-node acceptance are pinned to a
  fingerprint and reused across re-integration.
- **Integration:** ordered `--no-ff` merges onto the feature branch; a safety
  rail refuses the default branch.

## Install

```bash
pi install ./                    # or: pi install git:github.com/ming6ao/intergent
pi                               # launch a coordinator session
```

`pi install` registers both tools (`campaign` for the coordinator, `ig` for
workers) and installs the bundled skill. Nothing else is needed: the tools
invoke the bundled engine, so there is no `pip install` and no `intergent` on
`PATH`.

## Quick start

In pi, the coordinator drives a design into landed work with the `campaign`
tool; each spawned worker uses the `ig` tool to declare scopes, edit, and
commit.

```bash
pi install ./                    # or git:/npm: intergent
pi                               # launch a coordinator session

# then, in the session:
#   campaign start <DESIGN.md>   feature branch + planner DAG
#   campaign ready               nodes whose dependencies are done
#   campaign spawn <node>        one-shot worker in its own worktree
#   campaign verify <node>       read-only verifier
#   campaign integrate <node>    land the verified candidate
#   campaign report --narrative "what changed / risks"
```

Under the hood the tools drive the bundled engine, whose six verbs are `start`,
`status`, `declare`, `commit`, `integrate`, `report`. See
[docs/orchestration.md](./docs/orchestration.md) and
[docs/implementation.md](./docs/implementation.md).

## Develop

The engine is Python 3.11+ with no runtime dependencies; the pi adapter is
TypeScript. Run the suite with:

```bash
python3 -m unittest discover -s tests -v
```

```
intergent/          engine: service, store, git/worktrees, leases, verifier
bin/intergent       CLI shim (runs without install)
integrations/pi/    pi package: campaign.ts tool, common.ts, agents/
tools/gpu.sh        GPU broker (verifier only)
tests/              unittest suite
```

When adding an engine action, update `intergent/surface.py` (source of truth)
and the CLI follows; the pi `campaign` tool drives the CLI. See
[docs/implementation.md](./docs/implementation.md).

## Documentation

Start here: **[docs/README.md](./docs/README.md)**

- [Overview](./docs/overview.md) · [Architecture](./docs/architecture.md) ·
  [Conflict engine](./docs/conflict-engine.md) · [Local plane](./docs/local-plane.md) ·
  [Local implementation](./docs/implementation.md) ·
  [Agent integration](./docs/agents.md) · [Orchestration](./docs/orchestration.md)
