"""Smoke tests for the CLI adapter.

It is generated from :mod:`sliceme.surface`; these tests pin the shared
action surface and the end-to-end flow.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BIN = REPO_ROOT / "bin" / "sliceme"

sys.path.insert(0, str(REPO_ROOT))
from sliceme import surface  # noqa: E402


def run_cli(args, cwd):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)
    return subprocess.run(
        [sys.executable, str(BIN), *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env=env,
    )


class CliTests(unittest.TestCase):
    def test_init_and_status_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp, check=True)
            subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=tmp, check=True)
            subprocess.run(["git", "config", "user.name", "T"], cwd=tmp, check=True)
            (root / "a.txt").write_text("hi\n")
            subprocess.run(["git", "add", "-A"], cwd=tmp, check=True)
            subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp, check=True)

            out = run_cli(["--json", "start", "--check", "ok=true"], root)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertTrue((root / ".sliceme" / "config.json").is_file())

            out = run_cli(["--json", "start", "--name", "alpha"], root)
            self.assertEqual(out.returncode, 0, out.stderr)

            out = run_cli(["status", "--json"], root)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertIn("units", json.loads(out.stdout))

    def test_init_bootstraps_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp, check=True)
            subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=tmp, check=True)
            subprocess.run(["git", "config", "user.name", "T"], cwd=tmp, check=True)
            (root / "a.txt").write_text("hi\n")
            subprocess.run(["git", "add", "-A"], cwd=tmp, check=True)
            subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp, check=True)

            # First call from the main checkout initialises the plane and a unit.
            out = run_cli(["--json", "start"], root)
            self.assertEqual(out.returncode, 0, out.stderr)
            first = json.loads(out.stdout)
            self.assertTrue(first["initialized"])
            self.assertTrue(first["created"])
            self.assertTrue((root / ".sliceme" / "config.json").is_file())
            worktree = Path(first["worktree"])
            self.assertTrue(worktree.is_dir())

            # Second call from inside the unit worktree is a no-op.
            out = run_cli(["--json", "start"], worktree)
            self.assertEqual(out.returncode, 0, out.stderr)
            again = json.loads(out.stdout)
            self.assertFalse(again["initialized"])
            self.assertFalse(again["created"])
            self.assertEqual(again["unit"], first["unit"])

            # A fresh call from the main checkout gets a distinct unit name.
            out = run_cli(["--json", "start"], root)
            self.assertEqual(out.returncode, 0, out.stderr)
            third = json.loads(out.stdout)
            self.assertFalse(third["initialized"])
            self.assertTrue(third["created"])
            self.assertNotEqual(third["unit"], first["unit"])

            # `init` remains a hidden alias for `start`.
            out = run_cli(["--json", "init"], worktree)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(json.loads(out.stdout)["unit"], first["unit"])

    def test_status_health_and_simulate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp, check=True)
            subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=tmp, check=True)
            subprocess.run(["git", "config", "user.name", "T"], cwd=tmp, check=True)
            (root / "a.txt").write_text("hi\n")
            subprocess.run(["git", "add", "-A"], cwd=tmp, check=True)
            subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp, check=True)
            run_cli(["--json", "start"], root)
            run_cli(["--json", "start", "--name", "alpha"], root)
            run_cli(["--json", "start", "--name", "beta"], root)

            out = run_cli(["--json", "status", "--health"], root)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertTrue(json.loads(out.stdout)["ok"])

            out = run_cli(["--json", "status", "--simulate", "--no-checks"], root)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertIn("waves", json.loads(out.stdout))

    def test_start_ignores_state_subdirectories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp, check=True)
            subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=tmp, check=True)
            subprocess.run(["git", "config", "user.name", "T"], cwd=tmp, check=True)
            (root / "a.txt").write_text("hi\n")
            subprocess.run(["git", "add", "-A"], cwd=tmp, check=True)
            subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp, check=True)

            out = run_cli(["--json", "start"], root)
            self.assertEqual(out.returncode, 0, out.stderr)
            worktree = Path(json.loads(out.stdout)["worktree"])

            # Nothing start created may show up as untracked/modified.
            status = subprocess.run(
                ["git", "status", "--porcelain"], cwd=tmp, capture_output=True, text=True, check=True
            )
            self.assertEqual(status.stdout.strip(), "")

            # Every state subdirectory is covered by the ignore rule.
            for rel in ("config.json", "state.db", "worktrees", "scratch"):
                ignored = subprocess.run(
                    ["git", "check-ignore", f".sliceme/{rel}"],
                    cwd=tmp,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(ignored.returncode, 0, f".sliceme/{rel} not ignored")

            # The unit worktree itself stays clean too.
            wt_status = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(worktree),
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(wt_status.stdout.strip(), "")

    def test_cwd_native_status_and_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp, check=True)
            subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=tmp, check=True)
            subprocess.run(["git", "config", "user.name", "T"], cwd=tmp, check=True)
            (root / "a.txt").write_text("hi\n")
            subprocess.run(["git", "add", "-A"], cwd=tmp, check=True)
            subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp, check=True)
            subprocess.run(["git", "checkout", "-q", "-b", "feat/x"], cwd=tmp, check=True)
            run_cli(["--json", "start", "--no-unit"], root)
            out = run_cli(["--json", "start", "--name", "alpha", "--base", "feat/x"], root)
            worktree = Path(json.loads(out.stdout)["worktree"])

            out = run_cli(["status", "--short"], worktree)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(out.stdout.strip(), "alpha")

            (worktree / "a.txt").write_text("changed\n")
            # `commit` commits and registers a prepared candidate in one call.
            out = run_cli(["--json", "commit", "-m", "change"], worktree)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(json.loads(out.stdout)["candidate"]["status"], "prepared")

            # The coordinator lands the verified candidate on the feature branch.
            out = run_cli(["--json", "integrate", "--node", "alpha"], root)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(json.loads(out.stdout)["results"][0]["status"], "landed")
            self.assertEqual(
                subprocess.run(
                    ["git", "show", "feat/x:a.txt"], cwd=tmp, capture_output=True, text=True
                ).stdout,
                "changed\n",
            )

    def test_campaign_no_unit_integrate_and_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp, check=True)
            subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=tmp, check=True)
            subprocess.run(["git", "config", "user.name", "T"], cwd=tmp, check=True)
            (root / "a.txt").write_text("hi\n")
            (root / "b.txt").write_text("hi\n")
            subprocess.run(["git", "add", "-A"], cwd=tmp, check=True)
            subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp, check=True)

            # Campaign bootstrap: check out the feature branch first; `start`
            # adopts it and never creates one.
            subprocess.run(["git", "checkout", "-q", "-b", "feat/x"], cwd=tmp, check=True)
            out = run_cli(
                ["--json", "start", "--no-unit", "--check", "ok=true"],
                root,
            )
            self.assertEqual(out.returncode, 0, out.stderr)
            boot = json.loads(out.stdout)
            self.assertIsNone(boot["unit"])

            # A plain `status` has no units and reports the feature branch.
            out = run_cli(["status", "--json"], root)
            self.assertEqual(out.returncode, 0, out.stderr)
            status = json.loads(out.stdout)
            self.assertEqual(status["feature_branch"], "feat/x")
            self.assertEqual(status["units"], [])

            # Build two candidates through the CLI, base = feature branch.
            for name, rel in (("w1", "a.txt"), ("w2", "b.txt")):
                out = run_cli(["--json", "start", "--name", name, "--base", "feat/x"], root)
                self.assertEqual(out.returncode, 0, out.stderr)
                worktree = Path(json.loads(out.stdout)["worktree"])
                (worktree / rel).write_text(f"{name}\n")
                out = run_cli(["--json", "commit", "-m", name], worktree)
                self.assertEqual(out.returncode, 0, out.stderr)

            out = run_cli(["--json", "integrate"], root)
            self.assertEqual(out.returncode, 0, out.stderr)
            results = json.loads(out.stdout)["results"]
            self.assertEqual([r["status"] for r in results], ["landed", "landed"])
            self.assertEqual(
                subprocess.run(
                    ["git", "show", "feat/x:a.txt"], cwd=tmp, capture_output=True, text=True
                ).stdout,
                "w1\n",
            )

            # Re-running integrate is a no-op.
            out = run_cli(["--json", "integrate"], root)
            self.assertEqual(json.loads(out.stdout)["results"], [])

            out = run_cli(["--json", "report", "--narrative", "landed both"], root)
            self.assertEqual(out.returncode, 0, out.stderr)
            report = json.loads(out.stdout)
            self.assertTrue(Path(report["path"]).name == "feat--x.report.md")
            self.assertIn("landed both", report["content"])

    def test_cli_surface_matches_registry(self):
        """The CLI subcommands are exactly the registry (plus aliases)."""
        import argparse

        from sliceme.cli import build_parser

        sub = next(
            a for a in build_parser()._actions if isinstance(a, argparse._SubParsersAction)
        )
        expected = {a.name for a in surface.ACTIONS}
        expected |= {alias for a in surface.ACTIONS for alias in a.aliases}
        self.assertEqual(set(sub.choices), expected)

    def test_cli_and_agent_surfaces_share_actions(self):
        """The pi extension's action list must match surface.ACTIONS."""
        ext = REPO_ROOT / "integrations" / "pi" / "sliceme.ts"
        text = ext.read_text(encoding="utf-8")
        match = re.search(r"SLICEME_ACTIONS\s*=\s*\[(.*?)\]\s*as const", text, re.DOTALL)
        self.assertIsNotNone(match, "SLICEME_ACTIONS not found in pi extension")
        names = re.findall(r'"([a-z_]+)"', match.group(1))
        self.assertEqual(names, [a.name for a in surface.ACTIONS])


if __name__ == "__main__":
    unittest.main()
