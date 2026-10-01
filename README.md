# Sliceme

*Slice a design into parallel coding agents.*

Sliceme turns a design document into a DAG of work, runs the non-conflicting
nodes in parallel, verifies each against a content fingerprint, and lands them
on a feature branch. Ownership is decided at plan time, so parallel agents never
author the same files.

- **Plan-time ownership** — nodes own directories; overlapping subtrees are
  serialized into waves.
- **Isolation** — each unit runs in a git worktree; same-wave nodes own disjoint
  directories, so a wave can share one checkout.
- **One executor** — a single serialized, sandboxed runner drains a check queue,
  so verifiers judge recorded evidence instead of each running the suite.
- **Verified integration** — ordered `--no-ff` merges onto the feature branch; a
  safety rail refuses the default branch.

## Install

```bash
pi install ./                    # or: pi install git:github.com/ming6ao/sliceme
pi
```

`/sliceme [DESIGN.md]` (default `DESIGN.md`) activates the `sliceme` and
`sliceme-unit` tools for the session and starts a campaign. The tools invoke the
bundled engine, so there is no `pip install` and no `sliceme` on `PATH`.

## Use

In pi, run `/sliceme DESIGN.md`. The coordinator then drives the campaign with
the `sliceme` tool:

```text
sliceme start <DESIGN.md>   planner -> dag.json + waves
sliceme ready               current-wave nodes whose dependencies are integrated
sliceme spawn <node>        one-shot worker
sliceme verify <node>       executor runs checks; a read-only verifier judges
sliceme integrate <node>    land the verified candidate
sliceme report              deterministic report
```

## Docs

- [Guide](./docs/guide.md) — model, ownership, orchestration, agent roles.
- [Reference](./docs/reference.md) — actions, modules, state, verification.
- [Workflow](./docs/workflow.md) — the campaign loop and worker contract.
- [Publishing](./docs/publishing.md) — packaging and release.

## Develop

The engine is dependency-free Python 3.11+; the pi adapter is TypeScript.

```bash
python3 -m unittest discover -s tests -v
```

When adding an engine action, update `sliceme/surface.py` (the source of truth);
the CLI and pi tools follow.
