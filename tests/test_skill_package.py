"""Guards the repository packaging contract: the Agent Skill and the pi package.

The repo is installable both with
``npx skills add ming6ao/intergent -g -y -a claude-code`` (root ``SKILL.md`` +
bundled CLI) and with ``pi install ./`` (a pi package with a ``package.json``
manifest). These tests fail if either structure regresses.
"""

import json
import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
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
        self.assertTrue(SKILL.is_file(), "root SKILL.md is required for `npx skills add`")
        data = _frontmatter(SKILL.read_text(encoding="utf-8"))
        self.assertEqual(data.get("name"), "intergent")
        self.assertTrue(data.get("description"), "description is required frontmatter")

    def test_bundled_cli_and_package_exist(self):
        self.assertTrue((REPO_ROOT / "bin" / "intergent").is_file())
        self.assertTrue((REPO_ROOT / "intergent" / "cli.py").is_file())

    def test_no_nested_duplicate_skill(self):
        # The skills CLI returns the root SKILL.md immediately; a duplicate in a
        # subdirectory only creates confusion.
        nested = list(REPO_ROOT.glob("skills/**/SKILL.md"))
        self.assertEqual(nested, [], f"unexpected nested skills: {nested}")

    def test_skill_documents_bundled_cli_path(self):
        text = SKILL.read_text(encoding="utf-8")
        self.assertIn("bin/intergent", text)

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
