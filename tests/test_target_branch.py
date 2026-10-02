"""Target-branch selection and the hard default-branch guard.

The target (feature) branch is chosen once at start as the current branch, an
existing branch, or a new branch.  Sliceme never commits to ``main``,
``master``, or the repository default branch, and there is no override.
"""

import subprocess
import tempfile
import unittest
from pathlib import Path

from sliceme.service import Service
from sliceme.util import SlicemeError, config_path


def run(*args, cwd):
    return subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, check=True)


class TargetBranchCase(unittest.TestCase):
    checks = [{"name": "ok", "command": "true", "required": True}]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        run("git", "init", "-q", "-b", "main", cwd=self.root)
        run("git", "config", "user.email", "t@example.com", cwd=self.root)
        run("git", "config", "user.name", "Tester", cwd=self.root)
        (self.root / "a.txt").write_text("hi\n")
        run("git", "add", "-A", cwd=self.root)
        run("git", "commit", "-qm", "initial", cwd=self.root)
        self.svc = None

    def tearDown(self):
        if self.svc is not None:
            self.svc.close()
        self.tmp.cleanup()

    def branch_exists(self, branch):
        return (
            subprocess.run(
                ["git", "rev-parse", "--verify", branch],
                cwd=self.root,
                capture_output=True,
                text=True,
            ).returncode
            == 0
        )

    def test_current_mode_records_the_checked_out_branch(self):
        run("git", "checkout", "-q", "-b", "feat/current", cwd=self.root)
        Service.init_plane(self.root, checks=self.checks)
        self.svc = Service(self.root)
        self.assertEqual(self.svc.config["target_branch"], "feat/current")
        self.assertEqual(self.svc.config["main_branch"], "feat/current")
        self.assertEqual(self.svc.config["default_branch"], "main")
        self.assertTrue(self.svc.config["worktree_branch"].startswith("sliceme/"))

    def test_existing_mode_adopts_a_named_branch(self):
        run("git", "checkout", "-q", "-b", "feat/existing", cwd=self.root)
        run("git", "checkout", "-q", "main", cwd=self.root)
        Service.init_plane(
            self.root,
            target_branch="feat/existing",
            target_mode="existing",
            checks=self.checks,
        )
        self.svc = Service(self.root)
        self.assertEqual(self.svc.config["target_branch"], "feat/existing")

    def test_new_mode_creates_the_target_branch(self):
        Service.init_plane(
            self.root,
            target_branch="feat/brand-new",
            target_mode="new",
            checks=self.checks,
        )
        self.svc = Service(self.root)
        self.assertEqual(self.svc.config["target_branch"], "feat/brand-new")
        self.assertTrue(self.branch_exists("feat/brand-new"))

    def test_missing_existing_branch_is_rejected(self):
        with self.assertRaises(SlicemeError):
            Service.init_plane(
                self.root,
                target_branch="feat/missing",
                target_mode="existing",
                checks=self.checks,
            )

    def test_target_is_persisted_for_the_whole_campaign(self):
        run("git", "checkout", "-q", "-b", "feat/persist", cwd=self.root)
        Service.init_plane(self.root, checks=self.checks)
        import json

        first = json.loads(config_path(self.root).read_text())
        self.svc = Service(self.root)
        second = self.svc.config
        self.assertEqual(first["target_branch"], second["target_branch"])
        self.assertEqual(first["worktree_branch"], second["worktree_branch"])

    def test_deliver_refuses_main_even_when_it_is_not_the_recorded_default(self):
        # Simulate a repository whose recorded default is a non-main branch:
        # main is still reserved and must be refused.
        run("git", "checkout", "-q", "-b", "develop", cwd=self.root)
        Service.init_plane(self.root, target_branch="develop", target_mode="existing",
                           checks=self.checks)
        cfg = config_path(self.root).read_text()
        self.assertIn("develop", cfg)
        # Retarget the plane at main and confirm delivery refuses it.
        run("git", "checkout", "-q", "main", cwd=self.root)
        Service.init_plane(
            self.root, target_branch="main", target_mode="existing", force=True,
            checks=self.checks,
        )
        self.svc = Service(self.root)
        with self.assertRaises(SlicemeError) as ctx:
            self.svc.deliver()
        self.assertIn("default branch", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
