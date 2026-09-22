"""Guards the repository-root Agent Skill package contract.

The repo is installable with `npx skills add ming6ao/intergent -g -y -a claude-code`
which means the root must contain a valid ``SKILL.md`` and the CLI it references
must be bundled. These tests fail if that structure regresses.
"""

import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL = REPO_ROOT / "SKILL.md"
PI_EXTENSION = REPO_ROOT / "integrations" / "pi" / "intergent.ts"

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

    def test_pi_extension_recovers_from_removed_worktree(self):
        # A session can outlive its unit worktree (it lands and is cleaned up).
        # The extension must drop the stale binding and re-bootstrap instead of
        # running every `ig` call in a deleted directory.
        text = PI_EXTENSION.read_text(encoding="utf-8")
        self.assertIn("bindingIsStale(unitCwd, existsSync)", text)
        self.assertIn("await bootstrap(ctx)", text)

    def test_retired_actions_are_gone(self):
        # `submit` and `verify` were folded into `handoff` + `review`.
        names = {a.name for a in surface.ACTIONS}
        self.assertNotIn("submit", names)
        self.assertNotIn("verify", names)
        self.assertIn("handoff", names)
        text = PI_EXTENSION.read_text(encoding="utf-8")
        self.assertNotIn('"submit"', text)
        self.assertNotIn('"verify"', text)
        self.assertIn('"handoff"', text)


if __name__ == "__main__":
    unittest.main()
