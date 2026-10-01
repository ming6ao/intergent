"""End-to-end local-plane tests against real git repositories.

The single-agent handoff/approval path is gone; verification and landing are
exercised through the campaign ``integrate`` action. Lease arbitration, expiry,
and state migration stay covered here.
"""

import subprocess
import tempfile
import unittest
from pathlib import Path

from intergent.service import Service


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

    def prepare(self, unit, *, scope="file:src/app.py", content="a = 1\n", message="change"):
        """Create a unit, declare, edit, commit, and register a candidate."""
        workspace = self.svc.create_workspace(unit, base="feat/x")
        self.svc.declare_intent(unit, operation="modify", scope_specs=[scope])
        self.write(workspace["worktree"], scope.split(":", 1)[1], content)
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


class LeaseTests(RepoCase):
    def test_destructive_second_queues_then_promotes(self):
        self.svc.create_workspace("alpha", base="feat/x")
        self.svc.create_workspace("beta", base="feat/x")
        alpha = self.svc.declare_intent(
            "alpha", operation="replace", scope_specs=["symbol:src/app.py#hello"]
        )
        self.assertEqual(alpha["status"], "granted")
        beta = self.svc.declare_intent(
            "beta", operation="replace", scope_specs=["symbol:src/app.py#hello"]
        )
        self.assertEqual(beta["status"], "queued")
        self.assertEqual(beta["blocker"], "alpha")
        promoted = self.svc.release("alpha")["promoted"]
        self.assertEqual(promoted, ["beta"])

    def test_destructive_vs_additive_needs_a_decision(self):
        self.svc.create_workspace("alpha", base="feat/x")
        self.svc.create_workspace("beta", base="feat/x")
        self.svc.declare_intent("alpha", operation="extend", scope_specs=["config:app.timeout"])
        result = self.svc.declare_intent(
            "beta", operation="replace", scope_specs=["config:app.timeout"]
        )
        self.assertEqual(result["status"], "needs_decision")
        self.assertEqual(result["options"], ["replan"])

    def test_hierarchical_overlap_queues(self):
        self.svc.create_workspace("alpha", base="feat/x")
        self.svc.create_workspace("beta", base="feat/x")
        self.svc.declare_intent("alpha", operation="replace", scope_specs=["file:src/app.py"])
        beta = self.svc.declare_intent(
            "beta", operation="modify", scope_specs=["symbol:src/app.py#hello"]
        )
        self.assertEqual(beta["status"], "queued")
        self.assertEqual(beta["blocker_node"], "file:src/app.py")

    def test_expired_lease_is_reaped(self):
        self.svc.create_workspace("alpha", base="feat/x")
        self.svc.create_workspace("beta", base="feat/x")
        import json
        import time

        from intergent.util import config_path

        cfg = json.loads(config_path(self.root).read_text())
        cfg["lease_ttl_seconds"] = 1
        config_path(self.root).write_text(json.dumps(cfg))
        self.svc.declare_intent("alpha", operation="replace", scope_specs=["file:src/app.py"])
        beta = self.svc.declare_intent("beta", operation="replace", scope_specs=["file:src/app.py"])
        self.assertEqual(beta["status"], "queued")
        time.sleep(1.2)
        # Any subsequent service call reaps the expired lease and promotes beta.
        self.svc.status()
        request = self.svc.store.get_lock_request_for_unit(
            int(self.svc.store.get_unit("beta")["id"])
        )
        self.assertEqual(request["status"], "granted")


class VerificationTests(RepoCase):
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

    def test_integrate_releases_lease_and_promotes(self):
        self.svc.create_workspace("alpha", base="feat/x")
        self.svc.create_workspace("beta", base="feat/x")
        self.svc.declare_intent("alpha", operation="replace", scope_specs=["file:src/app.py"])
        self.svc.declare_intent("beta", operation="replace", scope_specs=["file:src/app.py"])
        unit = self.svc.store.get_unit("alpha")
        self.write(unit["worktree"], "src/app.py", "alpha = 1\n")
        self.svc.commit("alpha", "alpha")
        self.svc.finish("alpha")
        self.svc.integrate(node="alpha")
        request = self.svc.store.get_lock_request_for_unit(
            int(self.svc.store.get_unit("beta")["id"])
        )
        self.assertEqual(request["status"], "granted")


class ReleaseTests(RepoCase):
    def test_release_retires_unit_and_gc_prunes_worktree(self):
        alpha = self.svc.create_workspace("alpha", base="feat/x")
        self.assertTrue(Path(alpha["worktree"]).exists())

        self.svc.release("alpha")
        self.assertEqual(self.svc.store.get_unit("alpha")["state"], "closed")

        removed = self.svc.gc()["removed_worktrees"]
        self.assertIn("alpha", removed)
        self.assertFalse(Path(alpha["worktree"]).exists())
        # Closed units may hold unmerged work, so their branch is retained.
        self.assertTrue(self.branch_exists(alpha["branch"]))

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
