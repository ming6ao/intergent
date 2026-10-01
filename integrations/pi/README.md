# Intergent + pi

The intergent pi package registers **one tool per role**:

| Tool | Who uses it | Actions |
|---|---|---|
| `campaign` | the **coordinator** | `start`, `status`, `ready`, `spawn`, `verify`, `integrate`, `report` |
| `ig` | **workers** | `start`, `status`, `commit`, `integrate`, `report` |

Both are thin forwarders to the bundled `intergent` CLI (resolved from the
package, or `INTERGENT_BIN`), so the engine stays a separate process and no
`pip install` is needed. `runSubagent` passes each agent's `tools:`
frontmatter to `pi --tools`, so a worker sees only `ig` — never `campaign` — and
the read-only verifier sees neither.

Both tools register **inactive**. A plain session never lists them and never
receives their prompt guidelines, so Intergent is only used when asked for.
`/skill:intergent <design.md>` is the single entry point: when the argument is
an existing design document, the skill turns `campaign` and `ig` on for that
session; without one the command is dropped with a warning.

Install the package, not the files:

```bash
# from a checkout, or from git / npm once published
pi install ./
# pi install git:github.com/ming6ao/intergent
# pi install npm:intergent
```

Then launch the coordinator with `pi` and run
`/skill:intergent <design.md>`. There is **no single-agent bootstrap**: a
session is only bound to a unit when the campaign `spawn` action (or the user)
creates one, and the tools stay inert until the skill is explicitly invoked.

The skill ships with the package; there is no separate skill install.

See the root [`SKILL.md`](../../SKILL.md) for the workflow and
[`docs/orchestration.md`](../../docs/orchestration.md) for the specification.
