"""Smoke tests for the CLI adapter and MCP stdio server."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BIN = REPO_ROOT / "bin" / "intergent"


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

            out = run_cli(["--json", "init", "--check", "ok=true"], root)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertTrue((root / ".intergent" / "config.json").is_file())

            out = run_cli(["--json", "workspace", "create", "alpha"], root)
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

    def test_cwd_native_workspace_current_and_declare(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp, check=True)
            subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=tmp, check=True)
            subprocess.run(["git", "config", "user.name", "T"], cwd=tmp, check=True)
            (root / "a.txt").write_text("hi\n")
            subprocess.run(["git", "add", "-A"], cwd=tmp, check=True)
            subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp, check=True)
            run_cli(["--json", "init"], root)
            out = run_cli(["--json", "workspace", "create", "alpha"], root)
            worktree = Path(json.loads(out.stdout)["worktree"])

            out = run_cli(["workspace", "current", "--short"], worktree)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(out.stdout.strip(), "alpha")

            out = run_cli(
                ["--json", "declare", "--operation", "modify", "--scope", "file:a.txt"],
                worktree,
            )
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(json.loads(out.stdout)["status"], "granted")

            (worktree / "a.txt").write_text("changed\n")
            out = run_cli(["commit", "-m", "change"], worktree)
            self.assertEqual(out.returncode, 0, out.stderr)
            out = run_cli(["--json", "finish"], worktree)
            self.assertEqual(out.returncode, 0, out.stderr)
            candidate = json.loads(out.stdout)["id"]
            out = run_cli(["--json", "verify", str(candidate)], worktree)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(json.loads(out.stdout)["status"], "passed")

    def test_mcp_tools_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp, check=True)
            subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=tmp, check=True)
            subprocess.run(["git", "config", "user.name", "T"], cwd=tmp, check=True)
            (root / "a.txt").write_text("hi\n")
            subprocess.run(["git", "add", "-A"], cwd=tmp, check=True)
            subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp, check=True)
            run_cli(["--json", "init"], root)

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
            names = {tool["name"] for tool in lines[1]["result"]["tools"]}
            self.assertIn("declare_intent", names)
            self.assertIn("create_workspace", names)


if __name__ == "__main__":
    unittest.main()
