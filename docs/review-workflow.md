# Review and submission

The human stays in control: nothing lands silently, and background work surfaces
here when it finishes or needs a decision.

## 1. Review unit = the candidate

A session/worker produces a branch, possibly many commits. Review the
**candidate** (one logical task) as a unit, with commit history visible — the PR
model, assembled by the tool.

## 2. Review packet

- scope-annotated diff (`replace symbol:Login`, `add file:docs/api.md`);
- verification evidence (commands, results, pinned fingerprint);
- conflict/wave status ("assigned wave 2, after #512; auto-rebased");
- provenance (agent/model, task, declared intents, recorded decisions);
- risk flags (HIGH conflicts, schema/migration, critical paths, large diffs).

## 3. Where review happens

- **Host-mediated (recommended for teams):** the tool opens/updates the PR and
  posts the packet; humans review/approve in GitHub/GitLab with existing branch
  protection. No new review UI.
- **Tool-mediated (local/solo):** a minimal `intergent review <candidate>`
  TUI/CLI, used only when there is no remote.

## 4. Gates, policies, pre-approval

- Required before landing: local verification passed, no unresolved HIGH
  conflict, a wave assignment exists.
- **Risk-based routing:** low-risk classes (docs, tests, formatting) may be
  policy-auto-approved; structural/schema/security require human review.
- **Pre-approval is essential for a queue to flow:** approve a candidate now; it
  lands automatically when its dependency/wave clears. Otherwise the user
  babysits every wait.
- **Override** (`intergent declare --unit U --decide override --reason "..."`) is
  explicit and audited; never silent.

## 5. Submission = batched, wave-ordered

Once candidates are approved:

1. Coordinator computes waves over all approved candidates by mergeability.
2. For each wave: merge into one integration tree → run CI **once** → land.
3. Update PRs/status; rebase and re-wave the remainder; notify on failures
   (bisect to the culprit).

Integration is host-mediated (required check + host merge queue does the merge)
or tool-mediated (an integration branch is applied in wave order and
fast-forwarded).

## 6. Typical workflows

### Solo, one agent

```bash
intergent start --check "tests=pytest -q"
# agent over MCP: start → declare → commit → verify → review
intergent status && intergent review <candidate>
```

### Solo, many agents

```bash
intergent start --name docs-agent --task "update API docs"
intergent start --name auth-agent --task "add scope check to Login"
# intents declared; overlapping scopes warn / queue / auto-rebase
intergent status --simulate   # merge all ready candidates, test combined tree
```

### Team

Candidates arrive as PRs; the coordinator builds the graph, posts the wave plan,
runs one CI per wave, lands green waves, and rebases later ones. Reviewers use
the host's PR review; pre-approval lets queued work flow unattended.

---

Prev: [Remote plane](./remote-plane.md) · Next: [Operations](./operations.md)
