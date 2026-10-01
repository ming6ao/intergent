# Intergent

> *interlock + agent* — coordination for parallel coding agents: one campaign
> DAG, a git worktree per worker, scope leases, and fingerprint-pinned
> integration onto a feature branch.

**Status:** implemented as a dependency-free Python reference
(see [docs/implementation.md](./docs/implementation.md)).

Intergent lets several coding agents work the same repository in parallel
without authoring conflicting changes, verifies each candidate against a
content fingerprint, and lands verified candidates one DAG node at a time on a
campaign feature branch.

- **Isolation:** one `git worktree` + branch per worker unit.
- **Leases:** declared scopes are granted, queued, or escalated deterministically.
- **Verification:** the plane's checks and each node's acceptance commands are
  pinned to a fingerprint and reused across re-integration.
- **Integration:** `--no-ff` merges onto the feature branch, ordered by wave
  mergeability; a safety rail refuses the default branch.

## Documentation

Start here: **[docs/README.md](./docs/README.md)**

- [Overview](./docs/overview.md) · [Architecture](./docs/architecture.md) ·
  [Conflict engine](./docs/conflict-engine.md) · [Local plane](./docs/local-plane.md) ·
  [**Local implementation**](./docs/implementation.md) ·
  [**Agent integration**](./docs/agents.md) ·
  [Orchestration](./docs/orchestration.md)

## Campaign quick start

`intergent` is implemented in [`intergent/`](./intergent) (dependency-free
Python 3.11+). A **coordinator** session drives the campaign; workers run in
their own worktrees.

```bash
# 1. campaign bootstrap: feature branch, no coordinator unit
./bin/intergent start --no-unit --main feat/example --base main

# 2. a worker node (its own worktree on the feature branch)
./bin/intergent start --name w1 --base feat/example
#    ... worker declares, edits, runs acceptance, commits ...

# 3. verify + land the verified candidate, then report
./bin/intergent integrate --node w1
./bin/intergent report --narrative "what changed / risks"
```

In pi the coordinator drives the same steps with the `campaign` tool
(`start → status/ready → spawn → verify → integrate → report`). See
[docs/orchestration.md](./docs/orchestration.md).

Six actions cover the whole lifecycle: `start`, `status`, `declare`, `commit`,
`integrate`, `report`. `start --no-unit --main <feature> --base <base>`
bootstraps a campaign; `declare` also does `--dry-run` checks and
`--renew`/`--release`; `integrate` takes `--node`, `--acceptance`, `--gpu`,
`--check-only`, and `--cleanup`; `status --health/--simulate/--gc` diagnoses.
The pi extension exposes exactly **one** tool whose `action` is one of these
verbs. See [docs/implementation.md](./docs/implementation.md).

### Run a campaign inside pi

- **pi** — the repository is a pi package shipping the `ig` and `campaign`
  tools plus the skill:

  ```bash
  pi install ./                                  # or git:/npm: intergent
  INTERGENT_AUTO_BOOTSTRAP=0 pi                  # launch the coordinator
  ```

- **Bundled skill**: [`SKILL.md`](./SKILL.md) and
  [integrations/pi/](./integrations/pi/README.md).
- Full guide: [docs/agents.md](./docs/agents.md).

Workers `declare`, edit, and `commit`; the coordinator runs the read-only
verifier and lands the candidate with `integrate`. Promotion from the feature
branch to the default branch stays a human `git` step.

### Install as an agent skill

```bash
npx skills add ming6ao/intergent -g -y -a pi
```

This repository **is** an Agent Skill: the root [`SKILL.md`](./SKILL.md) bundles
the CLI (`bin/intergent` + the `intergent/` package), so no separate
`pip install` is required. The same repo is a **pi package** (`package.json`)
that installs the tools and the skill together with `pi install`. See
[docs/agents.md](./docs/agents.md).

```
CLI:     intergent  (alias: ig)
```
