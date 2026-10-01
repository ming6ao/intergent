"""Guards the pi package contract.

Intergent supports exactly one install path: ``pi install`` of this package,
which registers the ``campaign``/``ig`` tools and ships the bundled skill and
engine. These tests fail if that structure regresses.
"""

import json
import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.md"
SKILL = REPO_ROOT / "SKILL.md"
PACKAGE = REPO_ROOT / "package.json"
PI_DIR = REPO_ROOT / "integrations" / "pi"
PI_EXTENSION = PI_DIR / "intergent.ts"
PI_CAMPAIGN = PI_DIR / "campaign.ts"
PI_COMMON = PI_DIR / "common.ts"

sys.path.insert(0, str(REPO_ROOT))
from intergent import surface  # noqa: E402


def _frontmatter(text: str) -> dict[str, str]:
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    if not match:
        raise AssertionError("SKILL.md is missing YAML frontmatter")
    data: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if not line.strip() or line.startswith((" ", "\t", "#")):
            continue
        if ":" in line:
            key, value = line.split(":", 1)
            data[key.strip()] = value.strip().strip('"')
    return data


class SkillPackageTests(unittest.TestCase):
    def test_root_skill_md_is_valid(self):
        self.assertTrue(SKILL.is_file(), "root SKILL.md is required as the pi package skill")
        data = _frontmatter(SKILL.read_text(encoding="utf-8"))
        self.assertEqual(data.get("name"), "intergent")
        self.assertTrue(data.get("description"), "description is required frontmatter")

    def test_bundled_cli_and_package_exist(self):
        self.assertTrue((REPO_ROOT / "bin" / "intergent").is_file())
        self.assertTrue((REPO_ROOT / "intergent" / "cli.py").is_file())

    def test_no_skills_sh_install_path(self):
        # pi is the only supported harness; the package installs the skill, so
        # there is no separate `npx skills add` path to document.
        for path in (README, SKILL, REPO_ROOT / "docs" / "agents.md", PI_DIR / "README.md"):
            self.assertNotIn("npx skills add", path.read_text(encoding="utf-8"))

    def test_no_dead_bootstrap_env_or_cli_alias(self):
        # `INTERGENT_AUTO_BOOTSTRAP` belonged to a removed auto-bootstrap path,
        # and the `ig` CLI alias is gone (no `bin/ig`, no console scripts);
        # neither should reappear in the docs.
        docs = (
            README,
            SKILL,
            REPO_ROOT / "docs" / "README.md",
            REPO_ROOT / "docs" / "overview.md",
            REPO_ROOT / "docs" / "implementation.md",
            REPO_ROOT / "docs" / "agents.md",
            REPO_ROOT / "docs" / "orchestration.md",
            PI_DIR / "README.md",
        )
        for path in docs:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("INTERGENT_AUTO_BOOTSTRAP", text, path)
            self.assertNotIn("alias: ig", text, path)

    def test_root_skill_is_explicit_invocation_only(self):
        # The skill must not auto-load just because a repo has a plane; the
        # user invokes it with `/skill:intergent`.
        data = _frontmatter(SKILL.read_text(encoding="utf-8"))
        self.assertEqual(data.get("disable-model-invocation"), "true")
        self.assertNotIn("repository has .intergent/config.json", data.get("description", ""))

    def test_pi_extension_is_a_thin_forwarder(self):
        # There is no single-agent bootstrap: the extension only registers the
        # `ig` tool and forwards to the CLI via the shared helpers.
        text = PI_EXTENSION.read_text(encoding="utf-8")
        self.assertIn('from "./common.ts"', text)
        self.assertIn("runIg(pi, ctx", text)
        self.assertNotIn("bindingIsStale", text)
        self.assertNotIn("bootstrap(ctx)", text)
        self.assertNotIn("before_agent_start", text)

    def test_pi_package_manifest(self):
        self.assertTrue(PACKAGE.is_file(), "package.json is required for `pi install`")
        manifest = json.loads(PACKAGE.read_text(encoding="utf-8"))
        self.assertIn("pi-package", manifest.get("keywords", []))
        pi = manifest.get("pi", {})
        extensions = pi.get("extensions", [])
        self.assertIn("./integrations/pi/intergent.ts", extensions)
        self.assertIn("./integrations/pi/campaign.ts", extensions)
        self.assertIn(".", pi.get("skills", []))
        # The shared helpers ship with the package and are imported by both tools.
        self.assertTrue(PI_COMMON.is_file())
        self.assertIn('from "./common.ts"', PI_CAMPAIGN.read_text(encoding="utf-8"))

    def test_subagent_tools_are_scoped(self):
        # runSubagent must pass the agent's `tools:` allowlist to `pi --tools`;
        # this keeps workers on the `ig` unit tool and away from `campaign`,
        # and gives the read-only verifier no Intergent tool at all.
        common = PI_COMMON.read_text(encoding="utf-8")
        self.assertIn('"--tools"', common)
        self.assertIn("agentFrontmatterValue", common)
        worker = _frontmatter((PI_DIR / "agents" / "worker.md").read_text(encoding="utf-8"))
        worker_tools = [t.strip() for t in worker["tools"].split(",")]
        self.assertIn("ig", worker_tools)
        self.assertNotIn("campaign", worker_tools)
        verifier = _frontmatter((PI_DIR / "agents" / "verifier.md").read_text(encoding="utf-8"))
        self.assertNotIn("ig", [t.strip() for t in verifier["tools"].split(",")])

    def test_no_console_scripts(self):
        # The engine is internal: it is invoked from the package, never
        # installed as a user-facing `intergent`/`ig` command.
        pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertNotIn("[project.scripts]", pyproject)

    def test_package_ships_the_bundled_cli(self):
        manifest = json.loads(PACKAGE.read_text(encoding="utf-8"))
        files = manifest.get("files", [])
        self.assertIn("bin/", files)
        self.assertIn("intergent/", files)
        self.assertIn("SKILL.md", files)

    def test_retired_actions_are_gone(self):
        # The single-agent path (`handoff`, human `review`) and the old
        # `submit`/`verify` verbs are retired; `integrate` is the landing action.
        names = {a.name for a in surface.ACTIONS}
        for gone in ("submit", "verify", "handoff", "review"):
            self.assertNotIn(gone, names)
        self.assertIn("integrate", names)
        text = PI_EXTENSION.read_text(encoding="utf-8")
        for gone in ("submit", "verify", "handoff", "review"):
            self.assertNotIn(f'"{gone}"', text)

    def test_campaign_actions_are_in_lockstep(self):
        names = [a.name for a in surface.ACTIONS]
        self.assertIn("integrate", names)
        self.assertIn("report", names)
        text = PI_EXTENSION.read_text(encoding="utf-8")
        match = re.search(r"IG_ACTIONS\s*=\s*\[(.*?)\]\s*as const", text, re.DOTALL)
        self.assertIsNotNone(match)
        self.assertEqual(re.findall(r'"([a-z_]+)"', match.group(1)), names)
        # The skill documents the campaign additions.
        skill = SKILL.read_text(encoding="utf-8")
        self.assertIn("`integrate`", skill)
        self.assertIn("`report`", skill)
        self.assertIn("--no-unit", skill)


if __name__ == "__main__":
    unittest.main()
