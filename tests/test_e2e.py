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
        # Legacy tests exercise the one-shot `direct` path; draft mode is covered
        # by DraftLandingTests below.
        self._set_landing(mode="direct")

    def _set_landing(self, *, mode=None, strategy=None):
        from intergent.util import config_path, read_json, write_json

        config = read_json(config_path(self.root))
        landing = dict(config.get("landing") or {})
        if mode is not None:
            landing["mode"] = mode
        if strategy is not None:
            landing["strategy"] = strategy
        config["landing"] = landing
        write_json(config_path(self.root), config)

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

    def test_landing_cleans_worktree_by_default(self):
        alpha = self.svc.create_workspace("alpha")
        self.svc.declare_intent("alpha", operation="modify", scope_specs=["file:src/app.py"])
        self.write(alpha["worktree"], "src/app.py", "a = 1\n")
        self.svc.commit("alpha", "a")
        candidate = self.svc.finish("alpha")["id"]
        self.svc.verify(candidate)
        self.svc.approve(candidate)
        self.svc.land([candidate])
        self.assertFalse(Path(alpha["worktree"]).exists())
        # The branch is retained, so the landed history stays reachable.
        branch = subprocess.run(
            ["git", "rev-parse", "--verify", alpha["branch"]],
            cwd=self.root,
            capture_output=True,
            text=True,
        )
        self.assertEqual(branch.returncode, 0, branch.stderr)

    def test_landing_keep_preserves_worktree(self):
        alpha = self.svc.create_workspace("alpha")
        self.svc.declare_intent("alpha", operation="modify", scope_specs=["file:src/app.py"])
        self.write(alpha["worktree"], "src/app.py", "a = 1\n")
        self.svc.commit("alpha", "a")
        candidate = self.svc.finish("alpha")["id"]
        self.svc.verify(candidate)
        self.svc.approve(candidate)
        self.svc.land([candidate], cleanup=False)
        self.assertTrue(Path(alpha["worktree"]).exists())

    def _land_one_with_commits(self, *messages):
        alpha = self.svc.create_workspace("alpha")
        self.svc.declare_intent("alpha", operation="modify", scope_specs=["file:src/app.py"])
        for i, message in enumerate(messages):
            self.write(alpha["worktree"], "src/app.py", "line = %d\n" % i)
            self.svc.commit("alpha", message)
        candidate = self.svc.finish("alpha")["id"]
        self.svc.verify(candidate)
        self.svc.approve(candidate)
        base = run("git", "rev-parse", "HEAD", cwd=self.root).stdout.strip()
        results = self.svc.land([candidate])
        return results[0], base

    def test_squash_landing_hides_worktree_history(self):
        result, base = self._land_one_with_commits("worktree one", "worktree two")
        self.assertEqual(result["status"], "landed")
        self.assertEqual(result["strategy"], "squash")
        # Exactly one new commit on main, and it has a single parent.
        added = run("git", "log", "--format=%H", f"{base}..HEAD", cwd=self.root).stdout.split()
        self.assertEqual(len(added), 1)
        parents = run("git", "rev-list", "--parents", "-n", "1", "HEAD", cwd=self.root).stdout.split()
        self.assertEqual(len(parents), 2)
        subjects = run("git", "log", "--format=%s", "-n", "10", cwd=self.root).stdout
        self.assertNotIn("worktree one", subjects)
        self.assertNotIn("worktree two", subjects)
        self.assertIn("line = 1", (self.root / "src" / "app.py").read_text())

    def test_merge_strategy_keeps_worktree_history(self):
        self._set_landing(strategy="merge")
        result, base = self._land_one_with_commits("worktree one", "worktree two")
        self.assertEqual(result["status"], "landed")
        self.assertEqual(result["strategy"], "merge")
        parents = run("git", "rev-list", "--parents", "-n", "1", "HEAD", cwd=self.root).stdout.split()
        self.assertEqual(len(parents), 3)
        subjects = run("git", "log", "--format=%s", "-n", "10", cwd=self.root).stdout
        self.assertIn("worktree one", subjects)
        self.assertIn("worktree two", subjects)


class DraftLandingTests(RepoCase):
    def setUp(self):
        super().setUp()
        self._set_landing(mode="draft")

    def _prepare(self, commits=1):
        alpha = self.svc.create_workspace("alpha")
        self.svc.declare_intent("alpha", operation="modify", scope_specs=["file:src/app.py"])
        for i in range(commits):
            self.write(alpha["worktree"], "src/app.py", f"line = {i}\n")
            self.svc.commit("alpha", f"worktree commit {i}")
        candidate = self.svc.finish("alpha")["id"]
        self.svc.verify(candidate)
        self.svc.approve(candidate)
        return candidate, alpha

    def test_land_stages_draft_then_commits(self):
        candidate, alpha = self._prepare(commits=2)
        base = run("git", "rev-parse", "HEAD", cwd=self.root).stdout.strip()
        results = self.svc.land([candidate])
        self.assertEqual(results[0]["status"], "drafted")
        draft = results[0]["draft"]
        self.assertIn("src/app.py", draft["files"])
        self.assertIn("code -n", draft["open_command"])
        # Main HEAD has not moved, but the draft is staged on the worktree.
        self.assertEqual(run("git", "rev-parse", "HEAD", cwd=self.root).stdout.strip(), base)
        self.assertIn("src/app.py", run("git", "status", "--porcelain", cwd=self.root).stdout)
        # A second land without --commit refuses while a draft is pending.
        with self.assertRaises(IntergentError):
            self.svc.land([candidate])
        # Commit the draft: one squashed, single-parent commit.
        committed = self.svc.land([], commit_draft=True)
        self.assertEqual(committed[0]["status"], "landed")
        added = run("git", "log", "--format=%H", f"{base}..HEAD", cwd=self.root).stdout.split()
        self.assertEqual(len(added), 1)
        parents = run("git", "rev-list", "--parents", "-n", "1", "HEAD", cwd=self.root).stdout.split()
        self.assertEqual(len(parents), 2)
        self.assertIn("line = 1", (self.root / "src" / "app.py").read_text())

    def test_abort_restores_main(self):
        candidate, alpha = self._prepare()
        base = run("git", "rev-parse", "HEAD", cwd=self.root).stdout.strip()
        self.svc.land([candidate])
        self.assertNotEqual(run("git", "status", "--porcelain", cwd=self.root).stdout.strip(), "")
        aborted = self.svc.land([], abort_draft=True)
        self.assertEqual(aborted[0]["status"], "aborted")
        self.assertEqual(run("git", "rev-parse", "HEAD", cwd=self.root).stdout.strip(), base)
        self.assertEqual(run("git", "status", "--porcelain", cwd=self.root).stdout.strip(), "")
        self.assertNotIn("line = 0", (self.root / "src" / "app.py").read_text())

    def test_conflicting_candidate_blocks_but_drafts_clean_prefix(self):
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
        statuses = [r["status"] for r in results]
        self.assertIn("drafted", statuses)
        self.assertIn("blocked", statuses)
        self.svc.land([], abort_draft=True)


class ReleaseTests(RepoCase):
    def test_release_retires_unit_and_gc_prunes_worktree(self):
        alpha = self.svc.create_workspace("alpha")
        self.assertTrue(Path(alpha["worktree"]).exists())

        self.svc.release("alpha")
        self.assertEqual(self.svc.store.get_unit("alpha")["state"], "released")

        removed = self.svc.gc()["removed_worktrees"]
        self.assertIn("alpha", removed)
        self.assertFalse(Path(alpha["worktree"]).exists())


if __name__ == "__main__":
    unittest.main()
