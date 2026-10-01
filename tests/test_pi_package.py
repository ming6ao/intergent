"""Guards the pi package contract.

Sliceme supports exactly one install path: ``pi install`` of this package, which
registers one extension exposing the ``sliceme``/``sliceme-unit`` tools and the
``/sliceme`` command. There is no separate skill. These tests fail if that
structure regresses.
"""

import json
import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.md"
WORKFLOW = REPO_ROOT / "docs" / "workflow.md"
PACKAGE = REPO_ROOT / "package.json"
PI_DIR = REPO_ROOT / "integrations" / "pi"
PI_UNIT = PI_DIR / "unit.ts"
PI_COORDINATOR = PI_DIR / "coordinator.ts"
PI_COMMON = PI_DIR / "common.ts"

sys.path.insert(0, str(REPO_ROOT))
from sliceme import surface  # noqa: E402


def _frontmatter(text: str) -> dict[str, str]:
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    if not match:
        raise AssertionError("file is missing YAML frontmatter")
    data: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if not line.strip() or line.startswith((" ", "\t", "#")):
            continue
        if ":" in line:
            key, value = line.split(":", 1)
            data[key.strip()] = value.strip().strip('"')
    return data


class PiPackageTests(unittest.TestCase):
    def test_workflow_doc_exists(self):
        # The workflow lives in docs/workflow.md (human docs); the in-session
        # guidance is the tools' promptGuidelines.
        self.assertTrue(WORKFLOW.is_file(), "docs/workflow.md is required")
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("# Sliceme workflow", text)

    def test_bundled_cli_and_package_exist(self):
        self.assertTrue((REPO_ROOT / "bin" / "sliceme").is_file())
        self.assertTrue((REPO_ROOT / "sliceme" / "cli.py").is_file())

    def test_no_alternate_install_path(self):
        # pi is the only supported harness; the package installs the extension,
        # so there is no separate `npx skills add` path to document.
        docs = (README, WORKFLOW, REPO_ROOT / "docs" / "guide.md", REPO_ROOT / "docs" / "reference.md")
        for path in docs:
            self.assertNotIn("npx skills add", path.read_text(encoding="utf-8"))

    def test_no_dead_bootstrap_env_or_cli_alias(self):
        # `SLICEME_AUTO_BOOTSTRAP` belonged to a removed auto-bootstrap path,
        # and the retired `ig` CLI alias is gone; neither should reappear.
        docs = (README, WORKFLOW, REPO_ROOT / "docs" / "guide.md", REPO_ROOT / "docs" / "reference.md")
        for path in docs:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("SLICEME_AUTO_BOOTSTRAP", text, path)
            self.assertNotIn("INTERGENT", text, path)
            self.assertNotIn("intergent", text, path)
            self.assertNotIn("alias: ig", text, path)
            self.assertNotIn("bin/ig", text, path)

    def test_sliceme_is_extension_only(self):
        # No skill: the extension owns the `/sliceme` command and both tools.
        manifest = json.loads(PACKAGE.read_text(encoding="utf-8"))
        self.assertEqual(manifest.get("pi", {}).get("skills", []), [])
        coordinator = PI_COORDINATOR.read_text(encoding="utf-8")
        self.assertIn('pi.registerCommand("sliceme"', coordinator)
        self.assertIn("sendUserMessage", coordinator)
        # The command activates both tools for the session.
        self.assertIn("setActiveTools", coordinator)
        # The old skill-trigger glue is gone.
        self.assertNotIn("pi.on(\"input\"", coordinator)
        self.assertNotIn("/skill:sliceme", coordinator)
        self.assertNotIn("SKILL_INVOCATION", coordinator)
        self.assertNotIn("hasDesignDocument", coordinator)

    def test_tools_register_inactive_and_are_activated_by_the_command(self):
        # Re-gated: a plain session does not advertise Sliceme; `/sliceme` turns
        # both tools on via `pi.setActiveTools`.
        for path in (PI_UNIT, PI_COORDINATOR):
            text = path.read_text(encoding="utf-8")
            self.assertIn("defaultActive: false", text, path)
            self.assertNotIn("defaultActive: true", text, path)

    def test_pi_extension_is_a_thin_forwarder(self):
        # There is no single-agent bootstrap: the extension only registers the
        # `sliceme-unit` tool and forwards to the CLI via the shared helpers.
        text = PI_UNIT.read_text(encoding="utf-8")
        self.assertIn('from "./common.ts"', text)
        self.assertIn("runSliceme(pi, ctx", text)
        self.assertNotIn("bindingIsStale", text)
        self.assertNotIn("bootstrap(ctx)", text)
        self.assertNotIn("before_agent_start", text)

    def test_pi_package_manifest(self):
        self.assertTrue(PACKAGE.is_file(), "package.json is required for `pi install`")
        manifest = json.loads(PACKAGE.read_text(encoding="utf-8"))
        self.assertIn("pi-package", manifest.get("keywords", []))
        pi = manifest.get("pi", {})
        extensions = pi.get("extensions", [])
        self.assertIn("./integrations/pi/unit.ts", extensions)
        self.assertIn("./integrations/pi/coordinator.ts", extensions)
        # The shared helpers ship with the package and are imported by both tools.
        self.assertTrue(PI_COMMON.is_file())
        self.assertIn('from "./common.ts"', PI_COORDINATOR.read_text(encoding="utf-8"))

    def test_subagent_tools_are_scoped(self):
        # runSubagent must pass the agent's `tools:` allowlist to `pi --tools`;
        # this keeps workers on the `sliceme-unit` tool and away from the
        # `sliceme` coordinator tool, and gives the read-only verifier neither.
        common = PI_COMMON.read_text(encoding="utf-8")
        self.assertIn('"--tools"', common)
        self.assertIn("agentFrontmatterValue", common)
        worker = _frontmatter((PI_DIR / "agents" / "worker.md").read_text(encoding="utf-8"))
        worker_tools = [t.strip() for t in worker["tools"].split(",")]
        self.assertIn("sliceme-unit", worker_tools)
        self.assertNotIn("sliceme", worker_tools)
        verifier = _frontmatter((PI_DIR / "agents" / "verifier.md").read_text(encoding="utf-8"))
        verifier_tools = [t.strip() for t in verifier["tools"].split(",")]
        self.assertNotIn("sliceme-unit", verifier_tools)
        self.assertNotIn("bash", verifier_tools, "the verifier must not run commands")

    def test_campaign_executor_and_gate_wiring(self):
        # The coordinator drives the single executor and the sandbox gate, and
        # the verifier judges recorded evidence rather than running a suite.
        coordinator = PI_COORDINATOR.read_text(encoding="utf-8")
        self.assertIn('"exec"', coordinator)
        self.assertIn("sandboxGate", coordinator)
        self.assertIn('["exec", "--validate"]', coordinator)
        self.assertIn("EXEC_KEYS", coordinator)
        verifier = (PI_DIR / "agents" / "verifier.md").read_text(encoding="utf-8")
        self.assertNotIn("tools/gpu.sh", verifier)
        self.assertIn("executor", verifier)

    def test_no_console_scripts(self):
        # The engine is internal: it is invoked from the package, never
        # installed as a user-facing `sliceme` command.
        pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertNotIn("[project.scripts]", pyproject)

    def test_package_ships_the_bundled_cli(self):
        manifest = json.loads(PACKAGE.read_text(encoding="utf-8"))
        files = manifest.get("files", [])
        self.assertIn("bin/", files)
        self.assertIn("sliceme/", files)
        self.assertIn("docs/", files)

    def test_retired_actions_are_gone(self):
        # The single-agent path (`handoff`, human `review`) and the old
        # `submit`/`verify` verbs are retired; `integrate` is the landing action.
        names = {a.name for a in surface.ACTIONS}
        for gone in ("submit", "verify", "handoff", "review", "declare"):
            self.assertNotIn(gone, names)
        self.assertIn("integrate", names)
        text = PI_UNIT.read_text(encoding="utf-8")
        for gone in ("submit", "verify", "handoff", "review", "declare"):
            self.assertNotIn(f'"{gone}"', text)

    def test_campaign_actions_are_in_lockstep(self):
        names = [a.name for a in surface.ACTIONS]
        self.assertIn("integrate", names)
        self.assertIn("report", names)
        text = PI_UNIT.read_text(encoding="utf-8")
        match = re.search(r"SLICEME_ACTIONS\s*=\s*\[(.*?)\]\s*as const", text, re.DOTALL)
        self.assertIsNotNone(match)
        self.assertEqual(re.findall(r'"([a-z_]+)"', match.group(1)), names)
        # The workflow doc documents the campaign additions.
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("`integrate`", workflow)
        self.assertIn("`report`", workflow)
        self.assertIn("--no-unit", workflow)

    def test_campaign_scheduler_is_wave_aware(self):
        # The coordinator projects the DAG into waves via the engine and gates
        # spawns on the current wave; the wave planner is a first-class module.
        coordinator_text = PI_COORDINATOR.read_text(encoding="utf-8")
        for needle in ("dag_waves", "currentWave", "readyWaveNodes", "advanceWaves"):
            self.assertIn(needle, coordinator_text)
        self.assertTrue((REPO_ROOT / "sliceme" / "ownership.py").is_file())
        self.assertTrue((REPO_ROOT / "tests" / "test_waves.py").is_file())


if __name__ == "__main__":
    unittest.main()
