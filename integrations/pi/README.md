# Intergent + pi

The intergent pi package ships two native tools:

| Tool | Who uses it |
|---|---|
| `ig` | campaign **workers**: `declare` → `commit`, and the coordinator's `start`/`status` |
| `campaign` | the **coordinator**: `start`, `status`, `ready`, `spawn`, `verify`, `integrate`, `report` |

The tools are thin forwarders to the bundled `intergent` CLI (resolved from the
package, or `INTERGENT_BIN`, or `PATH`). Install the package, not the files:

```bash
# from a checkout, or from git / npm once published
pi install ./
# pi install git:github.com/ming6ao/intergent
# pi install npm:intergent
```

Then launch the coordinator with `INTERGENT_AUTO_BOOTSTRAP=0` and use the
`campaign` tool. There is **no single-agent bootstrap**: a session is only bound
to a unit when the campaign `spawn` action (or the user) creates one.

See the root [`SKILL.md`](../../SKILL.md) for the workflow and
[`docs/orchestration.md`](../../docs/orchestration.md) for the specification.
