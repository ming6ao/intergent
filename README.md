# Sliceme

> *slice the design into parallel agents* — coordination for parallel coding
> agents: one campaign DAG, a git worktree per worker, plan-time directory
> ownership, and fingerprint-pinned integration onto a feature branch.

Sliceme lets several coding agents work the same repository in parallel
without authoring conflicting changes, verifies each candidate against a
content fingerprint, and lands verified candidates one DAG node at a time on a
campaign feature branch. It is a dependency-free Python 3.11+ engine
([`sliceme/`](./sliceme)) plus a pi coordinator tool.

- **Isolation:** one `git worktree` + branch per worker unit.
- **Ownership:** nodes own directories at plan time; overlapping subtrees are
  serialized into waves, and `commit` rejects paths outside the owned dirs.
- **Verification:** plane checks and per-node acceptance are pinned to a
  fingerprint and reused across re-integration.
- **Integration:** ordered `--no-ff` merges onto the feature branch; a safety
  rail refuses the default branch.

## Install

```bash
pi install ./                    # or: pi install git:github.com/ming6ao/sliceme
pi                               # launch a coordinator session
```

`pi install` registers both tools (`campaign` for the coordinator, `sliceme` for
workers) and installs the bundled skill. Both tools are registered **inactive**:
a plain session never lists Sliceme, and
`/skill:sliceme <DESIGN.md>` turns them on for that session (the design path
must exist). Nothing else is needed: the tools invoke the bundled engine, so
there is no `pip install` and no `sliceme` on `PATH`.

## Quick start

In pi, run `/skill:sliceme <DESIGN.md>` to start a coordinator session; the
coordinator then drives the design into landed work with the `campaign` tool,
and each spawned worker uses the `sliceme` tool to edit its owned directories and
commit.

```bash
pi install ./                    # or git:/npm: sliceme
pi                               # launch a session

# then, in the session:
#   /skill:sliceme DESIGN.md   activate Sliceme for this session
#   campaign start <DESIGN.md>   adopt current branch + planner DAG
#   campaign ready               current-wave nodes whose dependencies are done
#   campaign spawn <node>        one-shot worker in its own worktree
#   campaign verify <node>       read-only verifier
#   campaign integrate <node>    land the verified candidate
#   campaign report --narrative "what changed / risks"
```

Under the hood the tools drive the bundled engine, whose five verbs are `start`,
`status`, `commit`, `integrate`, `report`. See [docs/guide.md](./docs/guide.md)
for the model and [docs/reference.md](./docs/reference.md) for the action
reference.

## Develop

The engine is Python 3.11+ with no runtime dependencies; the pi adapter is
TypeScript. Run the suite with:

```bash
python3 -m unittest discover -s tests -v
```

```
sliceme/          engine: service, store, git/worktrees, ownership, verifier,
                    integrate (landing + wave ordering + simulation), campaign
bin/sliceme       CLI shim (runs without install)
integrations/pi/    pi package: campaign.ts tool, sliceme tool, common.ts, agents/
tools/gpu.sh        GPU broker (verifier only)
tests/              unittest suite
```

When adding an engine action, update `sliceme/surface.py` (source of truth);
the CLI follows, and the pi tools drive the CLI. See
[docs/reference.md](./docs/reference.md).

## Documentation

- [Guide](./docs/guide.md) — model, directory ownership, orchestration, and
  agent integration.
- [Reference](./docs/reference.md) — actions, modules, state layout,
  verification, tests, and gaps.
