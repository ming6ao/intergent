"""End-to-end tests for the local plane against real git repositories."""

import subprocess
import tempfile
import unittest
from pathlib import Path

from intergent.service import Service
from intergent.util import IntergentError


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
        workspace = self.svc.create_workspace(unit)
        self.svc.declare_intent(unit, operation="modify", scope_specs=[scope])
        self.write(workspace["worktree"], scope.split(":", 1)[1], content)
        self.svc.commit(unit, message)
        candidate = self.svc.finish(unit)
        return workspace, candidate


class HappyPathTests(RepoCase):
    def test_happy_path_hands_off_and_approves_both_candidates(self):
        alpha = self.svc.create_workspace("alpha", agent="a")
        beta = self.svc.create_workspace("beta", agent="b")
        self.assertEqual(
            self.svc.declare_intent("alpha", operation="modify", scope_specs=["symbol:src/app.py#hello"])["status"],
            "granted",
        )
        self.assertEqual(
            self.svc.declare_intent("beta", operation="modify", scope_specs=["file:docs/api.md"])["status"],
            "granted",
        )
        self.write(alpha["worktree"], "src/app.py", "def hello():\n    return 'hello'\n")
        self.write(beta["worktree"], "docs/api.md", "# Docs\n\nAPI.\n")
        self.svc.commit("alpha", "improve hello")
        self.svc.commit("beta", "expand docs")
        self.svc.finish("alpha")
        self.svc.finish("beta")

        staged = self.svc.handoff()
        self.assertEqual([r["status"] for r in staged], ["pending"])
        # Nothing is committed yet: main shows staged changes only.
        self.assertNotEqual(
            run("git", "status", "--porcelain", cwd=self.root).stdout.strip(), ""
        )
        landed = self.svc.finalize(approve=True)
        self.assertEqual([r["status"] for r in landed], ["landed", "landed"])
        self.assertIn("hello", (self.root / "src" / "app.py").read_text())
        self.assertIn("API.", (self.root / "docs" / "api.md").read_text())

    def test_default_worktree_and_branch_collapse_duplicate_name(self):
        # No explicit session: the session defaults to the unit name, and the
        # worktree/branch must not repeat it ("solo-solo", "ig/solo/solo").
        unit = self.svc.create_workspace("solo")
        self.assertEqual(Path(unit["worktree"]).name, "solo")
        self.assertEqual(unit["branch"], "ig/solo")
        # A distinct explicit session still namespaces both.
        other = self.svc.create_workspace("solo", session="team")
        self.assertEqual(Path(other["worktree"]).name, "team-solo")
        self.assertEqual(other["branch"], "ig/team/solo")


class LeaseTests(RepoCase):
    def test_destructive_second_queues_then_promotes(self):
        self.svc.create_workspace("alpha")
        self.svc.create_workspace("beta")
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

    def test_destructive_vs_additive_requires_decision_and_override(self):
        self.svc.create_workspace("alpha")
        self.svc.create_workspace("beta")
        self.svc.declare_intent("alpha", operation="extend", scope_specs=["config:app.timeout"])
        result = self.svc.declare_intent(
            "beta", operation="replace", scope_specs=["config:app.timeout"]
        )
        self.assertEqual(result["status"], "needs_decision")
        self.assertEqual(result["options"], ["wait", "redesign", "override"])
        overridden = self.svc.decide(result["intent_id"], "override", reason="approved")
        self.assertEqual(overridden["status"], "granted")
        self.assertTrue(overridden["overridden"])

    def test_hierarchical_overlap_queues(self):
        self.svc.create_workspace("alpha")
        self.svc.create_workspace("beta")
        self.svc.declare_intent("alpha", operation="replace", scope_specs=["file:src/app.py"])
        beta = self.svc.declare_intent(
            "beta", operation="modify", scope_specs=["symbol:src/app.py#hello"]
        )
        self.assertEqual(beta["status"], "queued")
        self.assertEqual(beta["blocker_node"], "file:src/app.py")

    def test_expired_lease_is_reaped(self):
        self.svc.create_workspace("alpha")
        self.svc.create_workspace("beta")
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
    def test_failed_check_blocks_handoff(self):
        import json

        from intergent.util import config_path

        cfg = json.loads(config_path(self.root).read_text())
        cfg["checks"] = [{"name": "needs-OK", "command": "test -f OK", "required": True}]
        config_path(self.root).write_text(json.dumps(cfg))

        alpha = self.svc.create_workspace("alpha")
        self.svc.declare_intent("alpha", operation="modify", scope_specs=["file:src/app.py"])
        self.write(alpha["worktree"], "src/app.py", "changed = True\n")
        self.svc.commit("alpha", "change")
        self.svc.finish("alpha")

        results = self.svc.handoff()
        self.assertEqual(results[0]["status"], "failed")
        # No draft is staged and the candidate stays prepared.
        self.assertIsNone(self.svc.pending_handoff()["draft"])
        self.assertEqual(self.svc.store.get_candidate("alpha")["status"], "prepared")

    def test_handoff_records_verification(self):
        self.prepare("alpha")
        self.svc.handoff()
        candidate = self.svc.store.get_candidate("alpha")
        verification = self.svc.store.latest_verification(int(candidate["id"]))
        self.assertEqual(verification["status"], "passed")

    def test_handoff_verification_is_cached(self):
        self.prepare("alpha")
        self.svc.handoff()
        self.svc.finalize(approve=False)
        self.svc.handoff()
        count = self.svc.store.conn.execute(
            "SELECT COUNT(*) AS c FROM verifications"
        ).fetchone()["c"]
        self.assertEqual(count, 1)

    def test_landing_releases_lease_and_promotes(self):
        self.svc.create_workspace("alpha")
        self.svc.create_workspace("beta")
        self.svc.declare_intent("alpha", operation="replace", scope_specs=["file:src/app.py"])
        self.svc.declare_intent("beta", operation="replace", scope_specs=["file:src/app.py"])
        unit = self.svc.store.get_unit("alpha")
        self.write(unit["worktree"], "src/app.py", "alpha = 1\n")
        self.svc.commit("alpha", "alpha")
        self.svc.finish("alpha")
        self.svc.handoff()
        self.svc.finalize(approve=True)
        request = self.svc.store.get_lock_request_for_unit(
            int(self.svc.store.get_unit("beta")["id"])
        )
        self.assertEqual(request["status"], "granted")


class HandoffTests(RepoCase):
    def test_handoff_stages_then_approve_commits(self):
        self.prepare("alpha", content="line = 1\n")
        base = run("git", "rev-parse", "HEAD", cwd=self.root).stdout.strip()
        results = self.svc.handoff()
        self.assertEqual(results[0]["status"], "pending")
        draft = results[0]["draft"]
        self.assertIn("src/app.py", draft["files"])
        self.assertIn("code -n", draft["open_command"])
        # Main HEAD has not moved, but the draft is staged on the worktree.
        self.assertEqual(run("git", "rev-parse", "HEAD", cwd=self.root).stdout.strip(), base)
        self.assertIn("src/app.py", run("git", "status", "--porcelain", cwd=self.root).stdout)
        # A second handoff refuses while a draft is pending.
        with self.assertRaises(IntergentError):
            self.svc.handoff()
        # Approve: one squashed, single-parent commit.
        committed = self.svc.finalize(approve=True)
        self.assertEqual(committed[0]["status"], "landed")
        added = run("git", "log", "--format=%H", f"{base}..HEAD", cwd=self.root).stdout.split()
        self.assertEqual(len(added), 1)
        parents = run("git", "rev-list", "--parents", "-n", "1", "HEAD", cwd=self.root).stdout.split()
        self.assertEqual(len(parents), 2)

    def test_reject_restores_main_and_unit(self):
        self.prepare("alpha", content="line = 1\n")
        base = run("git", "rev-parse", "HEAD", cwd=self.root).stdout.strip()
        self.svc.handoff()
        self.assertNotEqual(run("git", "status", "--porcelain", cwd=self.root).stdout.strip(), "")
        rejected = self.svc.finalize(approve=False)
        self.assertEqual(rejected[0]["status"], "rejected")
        self.assertEqual(run("git", "rev-parse", "HEAD", cwd=self.root).stdout.strip(), base)
        self.assertEqual(run("git", "status", "--porcelain", cwd=self.root).stdout.strip(), "")
        self.assertEqual(self.svc.store.get_candidate("alpha")["status"], "prepared")
        self.assertEqual(self.svc.store.get_unit("alpha")["state"], "working")

    def test_approve_cleans_worktree_by_default(self):
        alpha, _ = self.prepare("alpha")
        self.svc.handoff()
        self.svc.finalize(approve=True)
        self.assertFalse(Path(alpha["worktree"]).exists())
        # The branch is retained, so the landed history stays reachable.
        branch = subprocess.run(
            ["git", "rev-parse", "--verify", alpha["branch"]],
            cwd=self.root,
            capture_output=True,
            text=True,
        )
        self.assertEqual(branch.returncode, 0, branch.stderr)

    def test_approve_keep_preserves_worktree(self):
        alpha, _ = self.prepare("alpha")
        self.svc.handoff()
        self.svc.finalize(approve=True, cleanup=False)
        self.assertTrue(Path(alpha["worktree"]).exists())

    def test_simulation_plans_waves(self):
        self.prepare("alpha", scope="file:src/app.py")
        self.prepare("beta", scope="file:docs/api.md")
        sim = self.svc.simulation(run_checks_flag=False)
        self.assertEqual(sim["candidate_count"], 2)
        self.assertEqual(sim["overall"], "pass")
        self.assertEqual(len(sim["waves"]), 1)

    def test_textual_conflict_stages_prefix_and_fails_second(self):
        alpha = self.svc.create_workspace("alpha")
        beta = self.svc.create_workspace("beta")
        self.svc.declare_intent("alpha", operation="modify", scope_specs=["file:src/app.py"])
        self.svc.declare_intent("beta", operation="modify", scope_specs=["file:src/app.py"])
        self.write(alpha["worktree"], "src/app.py", "line = 'alpha'\n")
        self.write(beta["worktree"], "src/app.py", "line = 'beta'\n")
        self.svc.commit("alpha", "alpha")
        self.svc.commit("beta", "beta")
        self.svc.finish("alpha")
        self.svc.finish("beta")

        results = self.svc.handoff()
        statuses = {r["status"] for r in results}
        self.assertIn("pending", statuses)
        self.assertIn("failed", statuses)
        # Only the clean prefix is staged; main is untouched until approval.
        self.svc.finalize(approve=True)
        self.assertIn("alpha", (self.root / "src" / "app.py").read_text())

    def _handoff_one_with_commits(self, *messages):
        alpha = self.svc.create_workspace("alpha")
        self.svc.declare_intent("alpha", operation="modify", scope_specs=["file:src/app.py"])
        for i, message in enumerate(messages):
            self.write(alpha["worktree"], "src/app.py", "line = %d\n" % i)
            self.svc.commit("alpha", message)
        self.svc.finish("alpha")
        base = run("git", "rev-parse", "HEAD", cwd=self.root).stdout.strip()
        self.svc.handoff()
        results = self.svc.finalize(approve=True)
        return results[0], base

    def test_squash_strategy_hides_worktree_history(self):
        result, base = self._handoff_one_with_commits("worktree one", "worktree two")
        self.assertEqual(result["status"], "landed")
        self.assertEqual(result["strategy"], "squash")
        added = run("git", "log", "--format=%H", f"{base}..HEAD", cwd=self.root).stdout.split()
        self.assertEqual(len(added), 1)
        parents = run("git", "rev-list", "--parents", "-n", "1", "HEAD", cwd=self.root).stdout.split()
        self.assertEqual(len(parents), 2)
        subjects = run("git", "log", "--format=%s", "-n", "10", cwd=self.root).stdout
        self.assertNotIn("worktree one", subjects)
        self.assertIn("line = 1", (self.root / "src" / "app.py").read_text())


class ReleaseTests(RepoCase):
    def test_release_retires_unit_and_gc_prunes_worktree(self):
        alpha = self.svc.create_workspace("alpha")
        self.assertTrue(Path(alpha["worktree"]).exists())

        self.svc.release("alpha")
        self.assertEqual(self.svc.store.get_unit("alpha")["state"], "closed")

        removed = self.svc.gc()["removed_worktrees"]
        self.assertIn("alpha", removed)
        self.assertFalse(Path(alpha["worktree"]).exists())


class StateMigrationTests(RepoCase):
    def test_legacy_states_are_collapsed_on_open(self):
        # Simulate a database written by the old lifecycle.
        alpha = self.svc.create_workspace("alpha")
        self.svc.store.set_unit_state(int(alpha["id"]), "finished")
        self.svc.store.conn.execute("UPDATE candidates SET status='approved'")
        self.svc.store.conn.commit()
        self.svc.close()

        self.svc = Service(self.root)
        self.assertEqual(self.svc.store.get_unit("alpha")["state"], "working")


if __name__ == "__main__":
    unittest.main()
