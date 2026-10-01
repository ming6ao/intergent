"""Executor queue, sandboxed runs, and fingerprint invalidation.

Phase 1 pins the engine-side executor: one serialized runner, a sandbox
profile folded into the fingerprint, dedupe of passing jobs, cancellation, and
crash-lease recovery.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from sliceme.sandbox import (
    Sandbox,
    backend_available,
    coerce_sandbox,
    resolve_sandbox,
    wrap_command,
)
from sliceme.service import Service
from sliceme.util import SlicemeError

REPO_ROOT = Path(__file__).resolve().parent.parent
BIN = REPO_ROOT / "bin" / "sliceme"


def run(*args, cwd):
    return subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, check=True)


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


class ExecutorCase(unittest.TestCase):
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
        Service.init_plane(self.root, checks=self.checks)
        self.svc = Service(self.root)
        self.executor = self.svc.executor()

    def tearDown(self):
        self.svc.close()
        self.tmp.cleanup()


class QueueTests(ExecutorCase):
    def submit(self, commands, **kwargs):
        return self.executor.submit(
            source=kwargs.pop("source", "node:w1"),
            commit=kwargs.pop("commit", "HEAD"),
            commands=commands,
            **kwargs,
        )

    def test_submit_run_passes_and_records_fingerprint(self):
        result = self.submit(["true"])
        job = result["job"]
        self.assertFalse(result["cached"])
        self.assertEqual(job["status"], "queued")
        self.assertTrue(job["fingerprint"])
        self.assertEqual(job["sandbox_digest"], Sandbox().digest())

        done = self.executor.drain()
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0]["status"], "passed")
        self.assertEqual(done[0]["exit_code"], 0)
        self.assertIn("[passed] acceptance[0]: true", done[0]["output"])

    def test_a_passing_job_is_deduped_not_rerun(self):
        first = self.submit(["true"])
        self.assertEqual(first["job"]["status"], "queued")
        executed = self.executor.drain()
        self.assertEqual(executed[0]["status"], "passed")

        second = self.submit(["true"])
        self.assertTrue(second["cached"])
        self.assertEqual(second["job"]["id"], first["job"]["id"])

        # Draining again does not re-run the cached job.
        self.assertEqual(self.executor.drain(), [])

    def test_failing_command_marks_the_job_failed(self):
        self.submit(["false"])
        done = self.executor.drain()
        self.assertEqual(done[0]["status"], "failed")
        self.assertNotEqual(done[0]["exit_code"], 0)

    def test_priority_orders_the_queue(self):
        low = self.submit(["true"], priority=0)
        high = self.submit(["true"], priority=5)
        first = self.executor.run_next()
        self.assertEqual(first["id"], high["job"]["id"])
        # The low-priority job remains queued.
        self.assertEqual(self.executor.store.get_job(low["job"]["id"])["status"], "queued")

    def test_timeout_is_persisted_and_fingerprinted(self):
        short_job = self.submit(["true"], timeout=10)["job"]
        long_job = self.submit(["true"], timeout=20)["job"]
        self.assertEqual(short_job["timeout"], 10)
        self.assertNotEqual(short_job["fingerprint"], long_job["fingerprint"])

    def test_cancel_only_cancels_queued_jobs(self):
        job = self.submit(["true"])
        cancelled = self.executor.cancel(job["job"]["id"])
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(self.executor.drain(), [])

    def test_recover_orphaned_running_job(self):
        job = self.submit(["true"])["job"]
        claimed = self.executor.store.claim_next_job(runner_pid=os.getpid())
        self.assertEqual(claimed["id"], job["id"])
        # Age the lease so recovery treats it as orphaned.
        self.executor.store.update_job(job["id"], started_at=0.0)
        self.executor.store.conn.commit()
        self.assertEqual(self.executor.recover_orphans(lease=1.0), 1)
        self.assertEqual(self.executor.store.get_job(job["id"])["status"], "queued")

    def test_submit_requires_source_commit_and_commands(self):
        with self.assertRaises(SlicemeError):
            self.executor.submit(source="", commit="HEAD", commands=["true"])
        with self.assertRaises(SlicemeError):
            self.executor.submit(source="node:w1", commit=None, commands=["true"])
        with self.assertRaises(SlicemeError):
            self.executor.submit(source="node:w1", commit="HEAD", commands=[])


class SandboxTests(unittest.TestCase):
    def test_default_is_unsandboxed_and_wraps_unchanged(self):
        self.assertEqual(wrap_command("echo hi", Sandbox()), "echo hi")

    def test_resolution_precedence(self):
        config = {"policy": {"sandbox": {"mode": "bwrap", "network": False}}}
        dag = {"sandbox": {"mode": "unshare"}}
        # Explicit override wins, then the DAG, then the plane policy.
        self.assertEqual(resolve_sandbox(dag, config, override="none").mode, "none")
        self.assertEqual(resolve_sandbox(dag, config).mode, "unshare")
        self.assertEqual(resolve_sandbox(None, config).mode, "bwrap")
        self.assertFalse(resolve_sandbox(None, config).network)
        self.assertEqual(resolve_sandbox(None, None).mode, "none")

    def test_coerce_rejects_unknown_mode(self):
        with self.assertRaises(SlicemeError):
            coerce_sandbox({"mode": "seccomp"})

    def test_digest_tracks_isolation_semantics(self):
        self.assertNotEqual(Sandbox().digest(), Sandbox(mode="bwrap").digest())
        self.assertNotEqual(
            Sandbox(mode="bwrap", network=True).digest(),
            Sandbox(mode="bwrap", network=False).digest(),
        )

    @unittest.skipUnless(backend_available("bwrap"), "bwrap not installed")
    def test_bwrap_wrap_is_well_formed(self):
        wrapped = wrap_command(
            "echo hi", Sandbox(mode="bwrap", network=False), worktree="/tmp/wt"
        )
        self.assertTrue(wrapped.startswith("bwrap "))
        self.assertIn("--unshare-net", wrapped)
        self.assertIn("--chdir /tmp/wt", wrapped)
        self.assertTrue(wrapped.endswith("-- /bin/sh -lc 'echo hi'"))


class FingerprintSandboxTests(ExecutorCase):
    def test_sandbox_digest_changes_the_fingerprint(self):
        plain = self.executor.submit(
            source="node:w1", commit="HEAD", commands=["true"], sandbox="none"
        )["job"]
        strict = self.executor.submit(
            source="node:w1", commit="HEAD", commands=["true"], sandbox="bwrap"
        )["job"]
        self.assertNotEqual(plain["fingerprint"], strict["fingerprint"])
        self.assertNotEqual(plain["sandbox_digest"], strict["sandbox_digest"])


class CliExecTests(ExecutorCase):
    def test_cli_exec_submit_run_wait(self):
        submitted = run_cli(
            [
                "--json",
                "exec",
                "--submit",
                "--source",
                "node:w1",
                "--commit",
                "HEAD",
                "--command",
                "true",
            ],
            self.root,
        )
        self.assertEqual(submitted.returncode, 0, submitted.stderr)
        job = json.loads(submitted.stdout)["job"]
        self.assertEqual(job["status"], "queued")

        drained = run_cli(["--json", "exec", "--run"], self.root)
        self.assertEqual(drained.returncode, 0, drained.stderr)
        self.assertEqual(json.loads(drained.stdout)["executed"], 1)

        waited = run_cli(["--json", "exec", "--wait", "--job", str(job["id"])], self.root)
        self.assertEqual(waited.returncode, 0, waited.stderr)
        self.assertEqual(json.loads(waited.stdout)["job"]["status"], "passed")

        status = run_cli(["--json", "exec"], self.root)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(json.loads(status.stdout)["counts"]["passed"], 1)


if __name__ == "__main__":
    unittest.main()
