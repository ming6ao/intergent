"""Campaign orchestration core: feature-branch integration, node verification,
report, and the ``dag.json``/``state.json`` helpers.

These pin the campaign contract:
* ``start --no-unit`` leaves no phantom unit and records the default branch;
* ``integrate`` lands prepared candidates on the feature branch, is idempotent,
  aborts conflicts atomically, and refuses the plane's default branch;
* node acceptance verdicts are fingerprinted with source ``node:<id>`` and
  reused (§6.4);
* ``sliceme report`` is a deterministic skeleton.
"""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from sliceme import campaign
from sliceme.service import Service
from sliceme.util import SlicemeError, config_path, write_json


def run(*args, cwd):
    return subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, check=True)


class CampaignCase(unittest.TestCase):
    checks = [{"name": "ok", "command": "true", "required": True}]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        run("git", "init", "-q", "-b", "main", cwd=self.root)
        run("git", "config", "user.email", "t@example.com", cwd=self.root)
        run("git", "config", "user.name", "Tester", cwd=self.root)
        (self.root / "src").mkdir()
        (self.root / "src" / "a.py").write_text("a = 1\n")
        (self.root / "src" / "b.py").write_text("b = 1\n")
        run("git", "add", "-A", cwd=self.root)
        run("git", "commit", "-qm", "initial", cwd=self.root)
        self.svc = None

    def tearDown(self):
        if self.svc is not None:
            self.svc.close()
        self.tmp.cleanup()

    def campaign_plane(self, branch="feat/x", base="main"):
        # `start` adopts the current branch, so check the campaign branch out
        # first instead of asking init_plane to create it.
        run("git", "checkout", "-q", "-b", branch, cwd=self.root)
        Service.init_plane(self.root, base=base, checks=self.checks)
        self.svc = Service(self.root)
        return self.svc

    def worker(self, name, rel, content, *, base="feat/x"):
        ws = self.svc.create_workspace(name, base=base)
        (Path(ws["worktree"]) / rel).write_text(content)
        self.svc.commit(name, name)
        self.svc.finish(name)
        return ws

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


class NoUnitBootstrapTests(CampaignCase):
    def test_no_unit_leaves_no_phantom_unit_and_records_default(self):
        run("git", "checkout", "-q", "-b", "feat/x", cwd=self.root)
        result = Service.init(self.root, no_unit=True)
        self.assertIsNone(result["unit"])
        self.assertIsNone(result["worktree"])
        service = Service(self.root)
        try:
            self.assertEqual(service.list_units(), [])
            self.assertTrue(self.branch_exists("feat/x"))
            self.assertEqual(service.config["main_branch"], "feat/x")
            self.assertEqual(service.config["default_branch"], "main")
        finally:
            service.close()

    def test_init_adopts_the_current_branch_and_never_creates_one(self):
        run("git", "checkout", "-q", "-b", "feat/adopt", cwd=self.root)
        Service.init(self.root, no_unit=True)
        service = Service(self.root)
        try:
            self.assertEqual(service.config["main_branch"], "feat/adopt")
            self.assertEqual(service.config["default_branch"], "main")
        finally:
            service.close()

    def test_init_rejects_a_missing_branch(self):
        with self.assertRaises(SlicemeError):
            Service.init(self.root, main_branch="feat/missing", no_unit=True)
        self.assertFalse(self.branch_exists("feat/missing"))

    def test_start_retargets_an_existing_plane_to_the_current_branch(self):
        Service.init(self.root, no_unit=True)  # plane starts on main
        run("git", "checkout", "-q", "-b", "feat/x", cwd=self.root)
        Service.init(self.root, main_branch="feat/x", no_unit=True)
        service = Service(self.root)
        try:
            self.assertEqual(service.config["main_branch"], "feat/x")
            self.assertEqual(service.config["base"], "feat/x")
            self.assertEqual(service.config["default_branch"], "main")
        finally:
            service.close()


class IntegrateTests(CampaignCase):
    def test_integrate_lands_candidates_and_is_idempotent(self):
        self.campaign_plane()
        self.worker("w1", "src/a.py", "a = 2\n")
        self.worker("w2", "src/b.py", "b = 2\n")

        first = self.svc.integrate()
        statuses = [r["status"] for r in first["results"]]
        self.assertEqual(statuses, ["landed", "landed"])
        self.assertEqual(self.file_on("feat/x", "src/a.py"), "a = 2\n")
        self.assertEqual(self.file_on("feat/x", "src/b.py"), "b = 2\n")
        head = run("git", "rev-parse", "feat/x", cwd=self.root).stdout.strip()

        second = self.svc.integrate()
        self.assertEqual(second["results"], [])
        self.assertEqual(run("git", "rev-parse", "feat/x", cwd=self.root).stdout.strip(), head)
        # One --no-ff merge commit per unit on top of the base.
        log = run("git", "log", "--format=%s", "main..feat/x", cwd=self.root).stdout
        self.assertEqual(log.count("sliceme integrate"), 2)
        # Branches are kept for provenance after integration.
        self.assertTrue(self.branch_exists("sliceme/w1"))
        self.assertTrue(self.branch_exists("sliceme/w2"))

    def test_integrate_refuses_the_default_branch(self):
        self.svc = Service(self.root)
        Service.init_plane(self.root, checks=self.checks)  # plain plane on main
        self.svc = Service(self.root)
        self.svc.create_workspace("alpha")
        Path(self.svc.store.get_unit("alpha")["worktree"], "src/a.py").write_text("a = 9\n")
        self.svc.commit("alpha", "alpha")
        self.svc.finish("alpha")
        with self.assertRaises(SlicemeError) as ctx:
            self.svc.integrate()
        self.assertIn("default branch", str(ctx.exception))

    def test_node_integrate_only_lands_that_node(self):
        self.campaign_plane()
        self.worker("w1", "src/a.py", "a = 2\n")
        self.worker("w2", "src/b.py", "b = 2\n")
        result = self.svc.integrate(node="w1")
        self.assertEqual([r["unit"] for r in result["results"]], ["w1"])
        self.assertEqual(self.file_on("feat/x", "src/a.py"), "a = 2\n")
        # w2 was not integrated, so its file still holds the base content.
        self.assertEqual(self.file_on("feat/x", "src/b.py"), "b = 1\n")

    def test_conflict_aborts_and_leaves_feature_untouched(self):
        self.campaign_plane()
        self.worker("w1", "src/a.py", "a = 'one'\n")
        self.worker("w2", "src/a.py", "a = 'two'\n")
        base_head = run("git", "rev-parse", "feat/x", cwd=self.root).stdout.strip()
        result = self.svc.integrate()
        self.assertEqual(result["results"][0]["status"], "landed")
        self.assertEqual(result["results"][1]["status"], "failed")
        self.assertIn("conflict", result["results"][1]["detail"])
        # The failed merge never advanced the feature branch past the first unit;
        # the tree is not half-merged.
        status = run("git", "status", "--porcelain", cwd=self.root).stdout
        self.assertNotIn("UU", status)
        self.assertNotEqual(
            run("git", "rev-parse", "feat/x", cwd=self.root).stdout.strip(), base_head
        )

    def test_integrate_cleanup_worktrees_prunes_landed_units(self):
        self.campaign_plane()
        ws = self.worker("w1", "src/a.py", "a = 2\n")
        result = self.svc.integrate(cleanup="worktrees")
        self.assertIsNotNone(result["cleanup"])
        self.assertFalse(Path(ws["worktree"]).exists())


class NodeVerificationTests(CampaignCase):
    def test_check_only_records_node_source_and_caches(self):
        self.campaign_plane()
        self.worker("w1", "src/a.py", "a = 2\n")
        first = self.svc.integrate(node="w1", acceptance=["true"], check_only=True)
        self.assertEqual(first["results"][0]["status"], "passed")
        candidate = self.svc.store.get_candidate("w1")
        verification = self.svc.store.latest_verification(int(candidate["id"]))
        self.assertEqual(verification["status"], "passed")
        self.assertEqual(verification["source"], "node:w1")
        self.assertEqual(verification["gpu"], "none")
        self.assertIn("true", verification["commands"])
        # The candidate is not landed in check-only mode.
        self.assertEqual(self.svc.store.get_candidate("w1")["status"], "prepared")

        before = self.svc.store.conn.execute("SELECT COUNT(*) AS c FROM verifications").fetchone()["c"]
        again = self.svc.integrate(node="w1", acceptance=["true"], check_only=True)
        self.assertEqual(again["results"][0]["status"], "passed")
        after = self.svc.store.conn.execute("SELECT COUNT(*) AS c FROM verifications").fetchone()["c"]
        self.assertEqual(before, after, "cached node verdict was not reused")

    def test_failing_node_acceptance_blocks_the_merge(self):
        self.campaign_plane()
        self.worker("w1", "src/a.py", "a = 2\n")
        result = self.svc.integrate(node="w1", acceptance=["false"])
        self.assertEqual(result["results"][0]["status"], "failed")
        self.assertEqual(self.svc.store.get_candidate("w1")["status"], "prepared")
        self.assertEqual(self.file_on("feat/x", "src/a.py"), "a = 1\n")

    def test_plane_and_node_fingerprints_do_not_collide(self):
        self.campaign_plane()
        self.worker("w1", "src/a.py", "a = 2\n")
        self.svc.integrate(node="w1", acceptance=["true"], check_only=True)
        candidate = self.svc.store.get_candidate("w1")
        rows = self.svc.store.conn.execute(
            "SELECT DISTINCT f.source AS source FROM verifications v"
            " JOIN fingerprints f ON f.id = v.fingerprint_id WHERE v.candidate_id=?",
            (int(candidate["id"]),),
        ).fetchall()
        sources = {r["source"] for r in rows}
        self.assertIn("node:w1", sources)


class ReportTests(CampaignCase):
    def test_report_is_deterministic(self):
        self.campaign_plane()
        self.worker("w1", "src/a.py", "a = 2\n")
        self.svc.integrate()
        write_json(
            campaign.dag_path(self.root, "feat/x"),
            {
                "campaign": "demo",
                "feature_branch": "feat/x",
                "base": "main",
                "design": "DESIGN.md",
                "concurrency": 3,
                "nodes": [
                    {
                        "id": "w1",
                        "label": "alpha",
                        "phase": "P0",
                        "goal": "g",
                        "owns": ["dir:src"],
                        "depends_on": [],
                        "acceptance": ["true"],
                        "gpu": "none",
                    }
                ],
            },
        )
        first = self.svc.report(narrative="Did the thing.")
        second = self.svc.report(narrative="Did the thing.")
        self.assertEqual(first["content"], second["content"])
        self.assertIn("Feature branch: feat/x", first["content"])
        self.assertIn("Design: DESIGN.md", first["content"])
        self.assertIn("w1", first["content"])
        self.assertIn("Did the thing.", first["content"])
        self.assertTrue(Path(first["path"]).is_file())
        self.assertEqual(Path(first["path"]).name, "feat--x.report.md")

    def test_status_exposes_campaign_fields(self):
        self.campaign_plane()
        self.worker("w1", "src/a.py", "a = 2\n")
        status = self.svc.status()
        self.assertEqual(status["feature_branch"], "feat/x")
        self.assertEqual(status["default_branch"], "main")
        unit = status["units"][0]
        self.assertEqual(unit["node"], "w1")
        self.assertTrue(unit["log"].endswith("feat--x.worker_w1.log"))
        self.assertIsNotNone(unit["candidate"])
        self.assertIn("verification", unit)


class DagWaveStatusTests(CampaignCase):
    def _dag(self, nodes, concurrency=2):
        write_json(
            campaign.dag_path(self.root, "feat/x"),
            {
                "campaign": "demo",
                "feature_branch": "feat/x",
                "base": "main",
                "concurrency": concurrency,
                "nodes": nodes,
            },
        )

    def test_status_projects_dag_waves_with_concurrency_cap(self):
        self.campaign_plane()
        self._dag(
            [
                {"id": "w1", "owns": ["dir:a"], "depends_on": []},
                {"id": "w2", "owns": ["dir:a"], "depends_on": []},
                {"id": "w3", "owns": ["dir:c"], "depends_on": ["w1"]},
                {"id": "w4", "owns": ["dir:d"], "depends_on": []},
            ]
        )
        status = self.svc.status()
        self.assertIsNone(status["dag_waves_error"])
        waves = status["dag_waves"]
        self.assertTrue(all(len(w["members"]) <= 2 for w in waves))
        assignment = {m: w["wave"] for w in waves for m in w["members"]}
        # w2 conflicts with w1 and w3 depends on w1, so both land in wave 1,
        # while w4 shares wave 0 with w1 within the concurrency cap.
        self.assertEqual(assignment, {"w1": 0, "w4": 0, "w2": 1, "w3": 1})

    def test_status_reports_a_cyclic_dag_without_crashing(self):
        self.campaign_plane()
        self._dag(
            [
                {"id": "w1", "owns": ["dir:a"], "depends_on": ["w2"]},
                {"id": "w2", "owns": ["dir:b"], "depends_on": ["w1"]},
            ]
        )
        status = self.svc.status()
        self.assertEqual(status["dag_waves"], [])
        self.assertIn("cycle", status["dag_waves_error"])


class DagStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_branch_key_replaces_slashes(self):
        self.assertEqual(campaign.branch_key("feat/nanochat-cpp"), "feat--nanochat-cpp")
        self.assertEqual(campaign.branch_key("main"), "main")

    def test_paths_share_one_glob_per_campaign(self):
        dag = campaign.dag_path(self.root, "feat/x")
        self.assertEqual(dag.name, "feat--x.dag.json")
        self.assertEqual(campaign.state_path(self.root, "feat/x").name, "feat--x.state.json")
        self.assertEqual(campaign.report_path(self.root, "feat/x").name, "feat--x.report.md")
        self.assertEqual(
            campaign.worker_log_path(self.root, "feat/x", "w1").name,
            "feat--x.worker_w1.log",
        )


class ConfigMigrationTests(CampaignCase):
    def test_default_branch_is_recorded_from_origin_head(self):
        # Simulate a remote default of ``main`` while checked out elsewhere.
        run("git", "checkout", "-q", "-b", "feat/x", cwd=self.root)
        run("git", "update-ref", "refs/remotes/origin/main", "main", cwd=self.root)
        run(
            "git",
            "symbolic-ref",
            "refs/remotes/origin/HEAD",
            "refs/remotes/origin/main",
            cwd=self.root,
        )
        Service.init_plane(self.root, checks=self.checks)
        cfg = json.loads(config_path(self.root).read_text())
        self.assertEqual(cfg["default_branch"], "main")

    def test_legacy_plane_without_default_branch_still_integrates(self):
        run("git", "checkout", "-q", "-b", "feat/x", cwd=self.root)
        Service.init_plane(self.root, checks=self.checks)
        cfg = json.loads(config_path(self.root).read_text())
        cfg.pop("default_branch", None)
        config_path(self.root).write_text(json.dumps(cfg))
        self.svc = Service(self.root)
        self.worker("w1", "src/a.py", "a = 2\n")
        result = self.svc.integrate()
        self.assertEqual(result["results"][0]["status"], "landed")


if __name__ == "__main__":
    unittest.main()
