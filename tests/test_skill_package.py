"""Guards the repository-root Agent Skill package contract.

The repo is installable with `npx skills add ming6ao/intergent -g -y -a claude-code`
which means the root must contain a valid ``SKILL.md`` and the CLI it references
must be bundled. These tests fail if that structure regresses.
"""

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL = REPO_ROOT / "SKILL.md"


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


if __name__ == "__main__":
    unittest.main()
