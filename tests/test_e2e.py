"""End-to-end local-plane tests against real git repositories.

The single-agent handoff/approval path and the lease system are gone;
verification and landing are exercised through the campaign ``integrate``
action, and plan conformance is enforced at commit time.
"""

import subprocess
import tempfile
import unittest
from pathlib import Path

from intergent import campaign
from intergent.service import Service
from intergent.util import IntergentError, write_json


def run(*args, cwd):
    return subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, check=True)


class RepoCase(unittest.TestCase):
    checks = [{"name": "ok", "command": "true", "required": True}]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        run("git", "init", "-q", "-b", "main", cwd=self.root)
        run("git", "config", "user.email", "t@example.com", cwd=self.root)
        run("git", "config", "user.name", "Tester", cwd=self.root)
        (self.root / "src").mkdir()
        (self.root / "docs").mkdir()
        (self.root / "src" / "app.py").write_text("def hello():\n    return 'hi'\n")
        (self.root / "docs" / "api.md").write_text("# Docs\n")
        run("git", "add", "-A", cwd=self.root)
        run("git", "commit", "-qm", "initial", cwd=self.root)
        # `start` adopts the current branch; check out the campaign branch first.
        run("git", "checkout", "-q", "-b", "feat/x", cwd=self.root)
        Service.init_plane(self.root, checks=self.checks)
        self.svc = Service(self.root)

    def tearDown(self):
        self.svc.close()
        self.tmp.cleanup()

    def write(self, worktree, rel, content):
        path = Path(worktree) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def write_dag(self, nodes):
        branch = self.svc.config["main_branch"]
        write_json(
            campaign.dag_path(self.root, branch),
            {"campaign": "t", "feature_branch": branch, "nodes": nodes},
        )

    def prepare(self, unit, *, rel="src/app.py", content="a = 1\n", message="change"):
        """Create a unit, edit, commit, and register a candidate."""
        workspace = self.svc.create_workspace(unit, base="feat/x")
        self.write(workspace["worktree"], rel, content)
        self.svc.commit(unit, message)
        candidate = self.svc.finish(unit)
        return workspace, candidate

    def file_on(self, branch, rel):
        return run("git", "show", f"{branch}:{rel}", cwd=self.root).stdout

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


class ConformanceTests(RepoCase):
    def test_commit_inside_owned_directory_is_accepted(self):
        self.write_dag([{"id": "alpha", "owns": ["dir:src"], "depends_on": []}])
        _, candidate = self.prepare("alpha", rel="src/app.py")
        self.assertEqual(candidate["status"], "prepared")

    def test_commit_outside_owned_directory_is_rejected(self):
        self.write_dag([{"id": "alpha", "owns": ["dir:src"], "depends_on": []}])
        workspace = self.svc.create_workspace("alpha", base="feat/x")
        self.write(workspace["worktree"], "docs/api.md", "# changed\n")
        self.svc.commit("alpha", "docs")
        with self.assertRaises(IntergentError) as ctx:
            self.svc.finish("alpha")
        self.assertIn("owned directories", str(ctx.exception))

    def test_no_dag_means_no_enforcement(self):
        # A non-campaign plane has no DAG, so any path is allowed.
        _, candidate = self.prepare("alpha", rel="docs/api.md", content="# x\n")
        self.assertEqual(candidate["status"], "prepared")

    def test_respawn_suffix_still_resolves_the_node(self):
        self.write_dag([{"id": "alpha", "owns": ["dir:src"], "depends_on": []}])
        workspace = self.svc.create_workspace("alpha-a2", base="feat/x")
        self.write(workspace["worktree"], "docs/api.md", "# changed\n")
        self.svc.commit("alpha-a2", "docs")
        with self.assertRaises(IntergentError):
            self.svc.finish("alpha-a2")


class IntegrationTests(RepoCase):
    def test_failed_check_blocks_integration(self):
        import json

        from intergent.util import config_path

        cfg = json.loads(config_path(self.root).read_text())
        cfg["checks"] = [{"name": "needs-OK", "command": "test -f OK", "required": True}]
        config_path(self.root).write_text(json.dumps(cfg))

        self.prepare("alpha", content="changed = True\n")
        results = self.svc.integrate()["results"]
        self.assertEqual(results[0]["status"], "failed")
        # The candidate stays prepared and the feature branch is untouched.
        self.assertEqual(self.svc.store.get_candidate("alpha")["status"], "prepared")
        self.assertEqual(self.file_on("feat/x", "src/app.py"), "def hello():\n    return 'hi'\n")

    def test_integrate_records_verification(self):
        self.prepare("alpha")
        self.svc.integrate()
        candidate = self.svc.store.get_candidate("alpha")
        verification = self.svc.store.latest_verification(int(candidate["id"]))
        self.assertEqual(verification["status"], "passed")
        self.assertEqual(verification["source"], "plane")

    def test_check_only_reuses_the_cached_fingerprint(self):
        self.prepare("alpha")
        self.svc.integrate(node="alpha", check_only=True)
        self.svc.integrate(node="alpha", check_only=True)
        count = self.svc.store.conn.execute(
            "SELECT COUNT(*) AS c FROM verifications"
        ).fetchone()["c"]
        self.assertEqual(count, 1)

    def test_integrate_lands_two_candidates(self):
        self.prepare("alpha", rel="src/app.py", content="alpha = 1\n")
        self.prepare("beta", rel="docs/api.md", content="# beta\n")
        results = self.svc.integrate()["results"]
        self.assertEqual([r["status"] for r in results], ["landed", "landed"])
        self.assertEqual(self.file_on("feat/x", "src/app.py"), "alpha = 1\n")
        self.assertEqual(self.file_on("feat/x", "docs/api.md"), "# beta\n")

    def test_integrate_orders_by_dag_wave(self):
        # beta's node depends on alpha, so alpha must land first even though
        # beta was prepared first.
        self.write_dag(
            [
                {"id": "alpha", "owns": ["dir:src"], "depends_on": []},
                {"id": "beta", "owns": ["dir:docs"], "depends_on": ["alpha"]},
            ]
        )
        self.prepare("beta", rel="docs/api.md", content="# beta\n")
        self.prepare("alpha", rel="src/app.py", content="alpha = 1\n")
        results = self.svc.integrate()["results"]
        self.assertEqual([r["unit"] for r in results], ["alpha", "beta"])


class GcTests(RepoCase):
    def test_gc_prunes_landed_branch(self):
        alpha, _ = self.prepare("alpha")
        self.svc.integrate()
        self.assertTrue(self.branch_exists(alpha["branch"]))

        result = self.svc.gc()
        self.assertIn(alpha["branch"], result["pruned_branches"])
        self.assertFalse(self.branch_exists(alpha["branch"]))


class StateMigrationTests(RepoCase):
    def test_legacy_states_are_collapsed_on_open(self):
        # Simulate a database written by the old lifecycle.
        alpha = self.svc.create_workspace("alpha", base="feat/x")
        self.svc.store.set_unit_state(int(alpha["id"]), "finished")
        self.svc.store.conn.execute("UPDATE candidates SET status='approved'")
        self.svc.store.conn.commit()
        self.svc.close()

        self.svc = Service(self.root)
        self.assertEqual(self.svc.store.get_unit("alpha")["state"], "working")


if __name__ == "__main__":
    unittest.main()
