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

    # ------------------------------------------------------------------


class HappyPathTests(RepoCase):
    def test_happy_path_lands_both_candidates(self):
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
        c_alpha = self.svc.finish("alpha")["id"]
        c_beta = self.svc.finish("beta")["id"]
        self.svc.verify(c_alpha)
        self.svc.verify(c_beta)
        self.svc.approve(c_alpha)
        self.svc.approve(c_beta)

        results = self.svc.land([c_alpha, c_beta])
        self.assertEqual([r["status"] for r in results], ["landed", "landed"])
        self.assertIn("hello", (self.root / "src" / "app.py").read_text())
        self.assertIn("API.", (self.root / "docs" / "api.md").read_text())


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
    def test_failed_check_blocks_approval(self):
        import json

        from intergent.util import config_path

        cfg = json.loads(config_path(self.root).read_text())
        cfg["checks"] = [{"name": "needs-OK", "command": "test -f OK", "required": True}]
        config_path(self.root).write_text(json.dumps(cfg))

        alpha = self.svc.create_workspace("alpha")
        self.svc.declare_intent("alpha", operation="modify", scope_specs=["file:src/app.py"])
        self.write(alpha["worktree"], "src/app.py", "changed = True\n")
        self.svc.commit("alpha", "change")
        candidate = self.svc.finish("alpha")["id"]
        result = self.svc.verify(candidate)
        self.assertEqual(result["status"], "failed")
        with self.assertRaises(IntergentError):
            self.svc.approve(candidate)

    def test_verification_is_cached_by_fingerprint(self):
        alpha = self.svc.create_workspace("alpha")
        self.svc.declare_intent("alpha", operation="modify", scope_specs=["file:src/app.py"])
        self.write(alpha["worktree"], "src/app.py", "changed = True\n")
        self.svc.commit("alpha", "change")
        candidate = self.svc.finish("alpha")["id"]
        first = self.svc.verify(candidate)
        second = self.svc.verify(candidate)
        self.assertFalse(first["from_cache"])
        self.assertTrue(second["from_cache"])

    def test_verification_released_lease_promotes(self):
        self.svc.create_workspace("alpha")
        self.svc.create_workspace("beta")
        self.svc.declare_intent("alpha", operation="replace", scope_specs=["file:src/app.py"])
        self.svc.declare_intent("beta", operation="replace", scope_specs=["file:src/app.py"])
        unit = self.svc.store.get_unit("alpha")
        self.write(unit["worktree"], "src/app.py", "alpha = 1\n")
        self.svc.commit("alpha", "alpha")
        candidate = self.svc.finish("alpha")["id"]
        self.svc.verify(candidate)
        request = self.svc.store.get_lock_request_for_unit(
            int(self.svc.store.get_unit("beta")["id"])
        )
        self.assertEqual(request["status"], "granted")


class LandingTests(RepoCase):
    def test_textual_conflict_blocks_second_and_preserves_main(self):
        alpha = self.svc.create_workspace("alpha")
        beta = self.svc.create_workspace("beta")
        self.svc.declare_intent("alpha", operation="modify", scope_specs=["file:src/app.py"])
        self.svc.declare_intent("beta", operation="modify", scope_specs=["file:src/app.py"])
        self.write(alpha["worktree"], "src/app.py", "line = 'alpha'\n")
        self.write(beta["worktree"], "src/app.py", "line = 'beta'\n")
        self.svc.commit("alpha", "alpha")
        self.svc.commit("beta", "beta")
        c_alpha = self.svc.finish("alpha")["id"]
        c_beta = self.svc.finish("beta")["id"]
        self.svc.verify(c_alpha)
        self.svc.verify(c_beta)
        self.svc.approve(c_alpha)
        self.svc.approve(c_beta)

        results = self.svc.land([c_alpha, c_beta])
        self.assertEqual(results[0]["status"], "landed")
        self.assertEqual(results[1]["status"], "blocked")
        self.assertIn("alpha", (self.root / "src" / "app.py").read_text())

    def test_simulation_plans_waves(self):
        alpha = self.svc.create_workspace("alpha")
        beta = self.svc.create_workspace("beta")
        self.svc.declare_intent("alpha", operation="modify", scope_specs=["file:src/app.py"])
        self.svc.declare_intent("beta", operation="modify", scope_specs=["file:docs/api.md"])
        self.write(alpha["worktree"], "src/app.py", "a = 1\n")
        self.write(beta["worktree"], "docs/api.md", "b\n")
        self.svc.commit("alpha", "a")
        self.svc.commit("beta", "b")
        self.svc.finish("alpha")
        self.svc.finish("beta")
        sim = self.svc.simulation(run_checks_flag=False)
        self.assertEqual(sim["candidate_count"], 2)
        self.assertEqual(sim["overall"], "pass")
        self.assertEqual(len(sim["waves"]), 1)

    def test_cleanup_removes_worktree(self):
        alpha = self.svc.create_workspace("alpha")
        self.svc.declare_intent("alpha", operation="modify", scope_specs=["file:src/app.py"])
        self.write(alpha["worktree"], "src/app.py", "a = 1\n")
        self.svc.commit("alpha", "a")
        candidate = self.svc.finish("alpha")["id"]
        self.svc.verify(candidate)
        self.svc.approve(candidate)
        self.svc.land([candidate], cleanup=True)
        self.assertFalse(Path(alpha["worktree"]).exists())


if __name__ == "__main__":
    unittest.main()
