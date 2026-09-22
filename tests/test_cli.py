"""Smoke tests for the CLI adapter and MCP stdio server.

Both are generated from :mod:`intergent.surface`; these tests pin the shared
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
BIN = REPO_ROOT / "bin" / "intergent"

sys.path.insert(0, str(REPO_ROOT))
from intergent import surface  # noqa: E402


def run_cli(args, cwd, *, input_text=None):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)
    return subprocess.run(
        [sys.executable, str(BIN), *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        input=input_text,
        env=env,
    )


class CliTests(unittest.TestCase):
    def test_init_declare_and_status_json(self):
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
            self.assertTrue((root / ".intergent" / "config.json").is_file())

            out = run_cli(["--json", "start", "--name", "alpha"], root)
            self.assertEqual(out.returncode, 0, out.stderr)

            out = run_cli(
                ["--json", "declare", "--unit", "alpha", "--operation", "modify",
                 "--scope", "file:a.txt"],
                root,
            )
            self.assertEqual(out.returncode, 0, out.stderr)
            payload = json.loads(out.stdout)
            self.assertEqual(payload["status"], "granted")

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
            out = run_cli(["--json", "start", "--agent", "pi"], root)
            self.assertEqual(out.returncode, 0, out.stderr)
            first = json.loads(out.stdout)
            self.assertTrue(first["initialized"])
            self.assertTrue(first["created"])
            self.assertTrue((root / ".intergent" / "config.json").is_file())
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

    def test_status_health_simulate_and_folded_override(self):
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

            run_cli(
                ["--json", "declare", "--unit", "alpha", "--operation", "extend",
                 "--scope", "config:app.timeout"],
                root,
            )
            out = run_cli(
                ["--json", "declare", "--unit", "beta", "--operation", "replace",
                 "--scope", "config:app.timeout"],
                root,
            )
            self.assertEqual(json.loads(out.stdout)["status"], "needs_decision")

            # `override` is folded into `declare --decide` (no intent id needed).
            out = run_cli(
                ["--json", "declare", "--unit", "beta", "--decide", "override",
                 "--reason", "approved"],
                root,
            )
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(json.loads(out.stdout)["status"], "granted")

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
                    ["git", "check-ignore", f".intergent/{rel}"],
                    cwd=tmp,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(ignored.returncode, 0, f".intergent/{rel} not ignored")

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
            run_cli(["--json", "start"], root)
            out = run_cli(["--json", "start", "--name", "alpha"], root)
            worktree = Path(json.loads(out.stdout)["worktree"])

            out = run_cli(["status", "--short"], worktree)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(out.stdout.strip(), "alpha")

            out = run_cli(
                ["--json", "declare", "--operation", "modify", "--scope", "file:a.txt"],
                worktree,
            )
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(json.loads(out.stdout)["status"], "granted")

            (worktree / "a.txt").write_text("changed\n")
            # `commit` commits and registers a prepared candidate in one call.
            out = run_cli(["--json", "commit", "-m", "change"], worktree)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(json.loads(out.stdout)["candidate"]["status"], "prepared")

            # `handoff` stages an uncommitted draft on main.
            out = run_cli(["--json", "handoff"], worktree)
            self.assertEqual(out.returncode, 0, out.stderr)
            draft = json.loads(out.stdout)["handoff"][0]["draft"]
            self.assertEqual(draft["files"], ["a.txt"])
            self.assertIn("-n", draft["open_command"])

            # `review` (human) shows the pending handoff.
            out = run_cli(["--json", "review"], root)
            self.assertEqual(out.returncode, 0, out.stderr)
            packet = json.loads(out.stdout)
            self.assertTrue(packet["pending"])
            self.assertEqual(packet["draft"]["files"], ["a.txt"])

            # `review --approve` commits the draft and cleans up the worktree.
            out = run_cli(["--json", "review", "--approve"], root)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(json.loads(out.stdout)["handoff"][0]["status"], "landed")
            self.assertFalse(worktree.exists())

    def test_approve_cleans_worktree_and_branch_and_keep_preserves_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp, check=True)
            subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=tmp, check=True)
            subprocess.run(["git", "config", "user.name", "T"], cwd=tmp, check=True)
            (root / "a.txt").write_text("hi\n")
            subprocess.run(["git", "add", "-A"], cwd=tmp, check=True)
            subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp, check=True)
            run_cli(["--json", "start"], root)

            def branch_exists(branch):
                return (
                    subprocess.run(
                        ["git", "rev-parse", "--verify", branch],
                        cwd=root,
                        capture_output=True,
                    ).returncode
                    == 0
                )

            def approve(*, keep):
                out = run_cli(["--json", "start", "--name", "alpha"], root)
                worktree = Path(json.loads(out.stdout)["worktree"])
                branch = subprocess.run(
                    ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                    cwd=worktree,
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout.strip()
                run_cli(
                    ["--json", "declare", "--operation", "modify", "--scope", "file:a.txt"],
                    worktree,
                )
                (worktree / "a.txt").write_text(f"changed keep={keep}\n")
                out = run_cli(["--json", "commit", "-m", "change"], worktree)
                self.assertEqual(out.returncode, 0, out.stderr)
                out = run_cli(["--json", "handoff"], worktree)
                self.assertEqual(out.returncode, 0, out.stderr)
                args = ["--json", "review", "--approve"]
                if keep:
                    args.append("--keep")
                out = run_cli(args, root)
                self.assertEqual(out.returncode, 0, out.stderr)
                return worktree, branch

            worktree, branch = approve(keep=False)
            self.assertFalse(worktree.exists())
            self.assertFalse(branch_exists(branch), branch)
            worktree, branch = approve(keep=True)
            self.assertTrue(worktree.exists())
            self.assertTrue(branch_exists(branch), branch)

    def test_mcp_exposes_one_action_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp, check=True)
            subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=tmp, check=True)
            subprocess.run(["git", "config", "user.name", "T"], cwd=tmp, check=True)
            (root / "a.txt").write_text("hi\n")
            subprocess.run(["git", "add", "-A"], cwd=tmp, check=True)
            subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp, check=True)
            run_cli(["--json", "start"], root)

            messages = "\n".join(
                [
                    json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
                    json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
                ]
            ) + "\n"
            out = run_cli(["mcp"], root, input_text=messages)
            self.assertEqual(out.returncode, 0, out.stderr)
            lines = [json.loads(line) for line in out.stdout.splitlines() if line.strip()]
            self.assertEqual(lines[0]["result"]["serverInfo"]["name"], "intergent")
            tools = lines[1]["result"]["tools"]
            self.assertEqual([t["name"] for t in tools], ["ig"])
            schema = tools[0]["inputSchema"]
            self.assertEqual(
                schema["properties"]["action"]["enum"],
                [a.name for a in surface.agent_actions()],
            )
            # Human-only actions and params never reach the agent.
            self.assertNotIn("review", schema["properties"]["action"]["enum"])
            self.assertNotIn("approve", schema["properties"])

    def test_mcp_call_and_human_action_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp, check=True)
            subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=tmp, check=True)
            subprocess.run(["git", "config", "user.name", "T"], cwd=tmp, check=True)
            (root / "a.txt").write_text("hi\n")
            subprocess.run(["git", "add", "-A"], cwd=tmp, check=True)
            subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp, check=True)
            run_cli(["--json", "start"], root)
            out = run_cli(["--json", "start", "--name", "alpha"], root)
            worktree = Path(json.loads(out.stdout)["worktree"])
            (worktree / "a.txt").write_text("changed\n")

            def call(msg_id, name, arguments):
                return json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "method": "tools/call",
                        "params": {"name": name, "arguments": arguments},
                    }
                )

            messages = "\n".join(
                [
                    json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
                    call(2, "ig", {"action": "declare", "unit": "alpha", "operation": "modify", "scopes": ["file:a.txt"]}),
                    call(3, "ig", {"action": "commit", "unit": "alpha", "message": "change", "summary": "s"}),
                    call(4, "ig", {"action": "status"}),
                    call(5, "ig", {"action": "status", "simulate": True, "no_checks": True}),
                    call(6, "review", {"approve": True}),
                ]
            ) + "\n"
            out = run_cli(["mcp"], root, input_text=messages)
            self.assertEqual(out.returncode, 0, out.stderr)
            lines = [json.loads(line) for line in out.stdout.splitlines() if line.strip()]
            payload = [json.loads(lines[i]["result"]["content"][0]["text"]) for i in (1, 2, 3, 4)]

            self.assertFalse(lines[1]["result"]["isError"])
            # commit commits and registers the candidate in one call.
            self.assertIn("commit", payload[1])
            self.assertIn("candidate", payload[1])
            self.assertTrue(payload[2]["candidates"])
            self.assertIn("waves", payload[3])
            # review is a human action and is refused for agents.
            self.assertTrue(lines[5]["result"]["isError"])
            self.assertIn("human action", lines[5]["result"]["content"][0]["text"])

    def test_cli_surface_matches_registry(self):
        """The CLI subcommands are exactly the registry (plus `mcp` and aliases)."""
        import argparse

        from intergent.cli import build_parser

        sub = next(
            a for a in build_parser()._actions if isinstance(a, argparse._SubParsersAction)
        )
        expected = {a.name for a in surface.ACTIONS} | {"mcp"}
        expected |= {alias for a in surface.ACTIONS for alias in a.aliases}
        self.assertEqual(set(sub.choices), expected)

    def test_cli_and_agent_surfaces_share_actions(self):
        """The pi extension's action list must match surface.agent_actions()."""
        ext = REPO_ROOT / "integrations" / "pi" / "intergent.ts"
        text = ext.read_text(encoding="utf-8")
        match = re.search(r"IG_ACTIONS\s*=\s*\[(.*?)\]\s*as const", text, re.DOTALL)
        self.assertIsNotNone(match, "IG_ACTIONS not found in pi extension")
        names = re.findall(r'"([a-z_]+)"', match.group(1))
        self.assertEqual(names, [a.name for a in surface.agent_actions()])


if __name__ == "__main__":
    unittest.main()
