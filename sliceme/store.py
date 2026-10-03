"""SQLite store for the local plane (WAL).

The local store is SQLite/WAL (``docs/reference.md`` §3).  The
service layer owns all business rules; this module owns persistence.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

from .util import SlicemeError, db_path, now

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE NOT NULL,
  task TEXT,
  attachment TEXT NOT NULL DEFAULT 'terminal',
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS units (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER REFERENCES sessions(id),
  name TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'worker',
  worktree TEXT NOT NULL,
  branch TEXT NOT NULL,
  base_commit TEXT,
  state TEXT NOT NULL DEFAULT 'working',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  UNIQUE(session_id, name)
);

CREATE TABLE IF NOT EXISTS candidates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  unit_id INTEGER NOT NULL REFERENCES units(id),
  branch TEXT NOT NULL,
  head_commit TEXT NOT NULL,
  base_commit TEXT,
  priority INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'prepared',
  summary TEXT,
  node TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS fingerprints (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  candidate_id INTEGER NOT NULL REFERENCES candidates(id),
  fingerprint TEXT NOT NULL,
  tree TEXT NOT NULL,
  cmd_digest TEXT NOT NULL,
  toolchain_digest TEXT NOT NULL,
  policy_digest TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'plane',
  created_at REAL NOT NULL,
  UNIQUE(candidate_id, fingerprint)
);

CREATE TABLE IF NOT EXISTS verifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  candidate_id INTEGER NOT NULL REFERENCES candidates(id),
  fingerprint_id INTEGER NOT NULL REFERENCES fingerprints(id),
  status TEXT NOT NULL,
  output TEXT,
  duration REAL,
  commands TEXT,
  gpu TEXT,
  created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_candidates_status ON candidates(status);

CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  wave INTEGER,
  requester TEXT,
  source TEXT NOT NULL,
  commit_ref TEXT NOT NULL,
  tree TEXT,
  commands TEXT NOT NULL,
  sandbox TEXT,
  sandbox_digest TEXT,
  gpu TEXT NOT NULL DEFAULT 'none',
  priority INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'queued',
  fingerprint TEXT,
  attempt INTEGER NOT NULL DEFAULT 0,
  timeout INTEGER NOT NULL DEFAULT 3600,
  requested_at REAL NOT NULL,
  started_at REAL,
  finished_at REAL,
  duration REAL,
  exit_code INTEGER,
  output TEXT,
  error TEXT,
  runner_pid INTEGER
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_fingerprint ON jobs(fingerprint, status);

CREATE TABLE IF NOT EXISTS campaign_sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  feature_branch TEXT,
  pi_session_id TEXT,
  session_file TEXT,
  label TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  reason TEXT,
  wave INTEGER,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  suspended_at REAL
);

CREATE INDEX IF NOT EXISTS idx_campaign_sessions_branch
  ON campaign_sessions(feature_branch);

CREATE TABLE IF NOT EXISTS attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  node TEXT NOT NULL,
  unit TEXT,
  attempt INTEGER NOT NULL DEFAULT 1,
  agent TEXT NOT NULL DEFAULT 'worker',
  status TEXT NOT NULL DEFAULT 'running',
  started_at REAL NOT NULL,
  finished_at REAL,
  duration REAL,
  exit_code INTEGER,
  turns INTEGER NOT NULL DEFAULT 0,
  tool_calls INTEGER NOT NULL DEFAULT 0,
  tools TEXT,
  tokens_in INTEGER NOT NULL DEFAULT 0,
  tokens_out INTEGER NOT NULL DEFAULT 0,
  cost REAL NOT NULL DEFAULT 0,
  last_tool TEXT,
  last_activity_at REAL,
  error TEXT
);

CREATE INDEX IF NOT EXISTS idx_attempts_node ON attempts(node);
CREATE INDEX IF NOT EXISTS idx_attempts_status ON attempts(status);
"""


def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


def _dicts(rows: Sequence[sqlite3.Row]) -> list[dict[str, Any]]:
    return [{k: r[k] for k in r.keys()} for r in rows]


class Store:
    def __init__(self, root: Path):
        self.root = root
        path = db_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), timeout=10.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Additive column migrations for planes created by older versions."""
        self._ensure_columns("jobs", {"timeout": "INTEGER NOT NULL DEFAULT 3600"})
        self._ensure_columns("candidates", {"node": "TEXT"})

    def _ensure_columns(self, table: str, columns: dict[str, str]) -> None:
        existing = {
            row["name"]
            for row in self.conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for name, decl in columns.items():
            if name not in existing:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # ---- sessions -----------------------------------------------------
    def create_session(self, name: str, task: str | None, attachment: str) -> int:
        with self.tx() as c:
            c.execute(
                "INSERT INTO sessions(name, task, attachment, created_at) VALUES(?,?,?,?)",
                (name, task, attachment, now()),
            )
            row = c.execute("SELECT id FROM sessions WHERE name=?", (name,)).fetchone()
        return int(row["id"])

    def get_session(self, name_or_id: str | int) -> dict[str, Any] | None:
        if isinstance(name_or_id, int) or str(name_or_id).isdigit():
            return _dict(
                self.conn.execute(
                    "SELECT * FROM sessions WHERE id=?", (int(name_or_id),)
                ).fetchone()
            )
        return _dict(
            self.conn.execute("SELECT * FROM sessions WHERE name=?", (name_or_id,)).fetchone()
        )

    # ---- units --------------------------------------------------------
    def create_unit(
        self,
        *,
        session_id: int,
        name: str,
        kind: str,
        worktree: str,
        branch: str,
        base_commit: str,
    ) -> int:
        ts = now()
        with self.tx() as c:
            c.execute(
                "INSERT INTO units(session_id, name, kind, worktree, branch, base_commit,"
                " state, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (session_id, name, kind, worktree, branch, base_commit, "working", ts, ts),
            )
            row = c.execute(
                "SELECT id FROM units WHERE session_id=? AND name=?", (session_id, name)
            ).fetchone()
        return int(row["id"])

    def set_unit_state(self, unit_id: int, state: str) -> None:
        self.conn.execute(
            "UPDATE units SET state=?, updated_at=? WHERE id=?", (state, now(), unit_id)
        )

    def get_unit(self, name_or_id: str | int) -> dict[str, Any] | None:
        if isinstance(name_or_id, int) or str(name_or_id).isdigit():
            row = self.conn.execute(
                "SELECT * FROM units WHERE id=?", (int(name_or_id),)
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT * FROM units WHERE name=? ORDER BY id DESC LIMIT 1", (name_or_id,)
            ).fetchone()
        return _dict(row)

    def list_units(self, *, active_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM units"
        if active_only:
            sql += " WHERE state='active'"
        sql += " ORDER BY id"
        return _dicts(self.conn.execute(sql).fetchall())

    # ---- candidates ---------------------------------------------------
    def create_candidate(
        self,
        *,
        unit_id: int,
        branch: str,
        head_commit: str,
        base_commit: str,
        priority: int,
        summary: str | None,
        node: str | None = None,
    ) -> int:
        ts = now()
        with self.tx() as c:
            c.execute(
                "INSERT INTO candidates(unit_id, branch, head_commit, base_commit,"
                " priority, status, summary, node, created_at, updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (unit_id, branch, head_commit, base_commit, priority, "prepared", summary, node, ts, ts),
            )
            return int(c.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])

    def update_candidate(
        self,
        candidate_id: int,
        *,
        status: str | None = None,
        head_commit: str | None = None,
        base_commit: str | None = None,
    ) -> None:
        sets = ["updated_at=?"]
        params: list[Any] = [now()]
        if status is not None:
            sets.append("status=?")
            params.append(status)
        if head_commit is not None:
            sets.append("head_commit=?")
            params.append(head_commit)
        if base_commit is not None:
            sets.append("base_commit=?")
            params.append(base_commit)
        params.append(candidate_id)
        self.conn.execute(f"UPDATE candidates SET {', '.join(sets)} WHERE id=?", params)

    def get_candidate(self, name_or_id: str | int) -> dict[str, Any] | None:
        text = str(name_or_id)
        if text.isdigit():
            row = self.conn.execute(
                "SELECT * FROM candidates WHERE id=?", (int(text),)
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT c.* FROM candidates c JOIN units u ON u.id=c.unit_id"
                " WHERE u.name=? OR c.node=? ORDER BY c.id DESC LIMIT 1",
                (text, text),
            ).fetchone()
        return _dict(row)

    def list_candidates(self, *, statuses: Sequence[str] | None = None) -> list[dict[str, Any]]:
        sql = (
            "SELECT c.*, u.name AS unit_name, u.worktree AS worktree"
            " FROM candidates c JOIN units u ON u.id=c.unit_id"
        )
        params: list[Any] = []
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            sql += f" WHERE c.status IN ({placeholders})"
            params.extend(statuses)
        sql += " ORDER BY c.priority DESC, c.created_at ASC, c.id ASC"
        return _dicts(self.conn.execute(sql, params).fetchall())

    # ---- fingerprints / verifications --------------------------------
    def get_or_create_fingerprint(
        self,
        candidate_id: int,
        fingerprint: str,
        tree: str,
        cmd_digest: str,
        toolchain_digest: str,
        policy_digest: str,
        source: str = "plane",
    ) -> int:
        row = self.conn.execute(
            "SELECT id FROM fingerprints WHERE candidate_id=? AND fingerprint=?",
            (candidate_id, fingerprint),
        ).fetchone()
        if row is not None:
            return int(row["id"])
        with self.tx() as c:
            c.execute(
                "INSERT INTO fingerprints(candidate_id, fingerprint, tree, cmd_digest,"
                " toolchain_digest, policy_digest, source, created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    candidate_id,
                    fingerprint,
                    tree,
                    cmd_digest,
                    toolchain_digest,
                    policy_digest,
                    source,
                    now(),
                ),
            )
            return int(c.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])

    def add_verification(
        self,
        candidate_id: int,
        fingerprint_id: int,
        status: str,
        output: str,
        duration: float,
        commands: Any = None,
        gpu: str | None = None,
    ) -> int:
        with self.tx() as c:
            c.execute(
                "INSERT INTO verifications(candidate_id, fingerprint_id, status, output,"
                " duration, commands, gpu, created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    candidate_id,
                    fingerprint_id,
                    status,
                    output,
                    duration,
                    json.dumps(commands) if commands is not None else None,
                    gpu,
                    now(),
                ),
            )
            return int(c.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])

    def latest_verification_for_fingerprint(
        self, fingerprint_id: int
    ) -> dict[str, Any] | None:
        return _dict(
            self.conn.execute(
                "SELECT * FROM verifications WHERE fingerprint_id=? ORDER BY id DESC LIMIT 1",
                (fingerprint_id,),
            ).fetchone()
        )

    def latest_verification(self, candidate_id: int) -> dict[str, Any] | None:
        return _dict(
            self.conn.execute(
                "SELECT v.*, f.source AS source, f.fingerprint AS fingerprint"
                " FROM verifications v JOIN fingerprints f ON f.id = v.fingerprint_id"
                " WHERE v.candidate_id=? ORDER BY v.id DESC LIMIT 1",
                (candidate_id,),
            ).fetchone()
        )

    def require_unit(self, name_or_id: str | int) -> dict[str, Any]:
        unit = self.get_unit(name_or_id)
        if unit is None:
            raise SlicemeError(f"unknown unit: {name_or_id}")
        return unit

    # ---- executor jobs ------------------------------------------------
    JOB_FIELDS = frozenset(
        {
            "wave",
            "requester",
            "source",
            "commit_ref",
            "tree",
            "commands",
            "sandbox",
            "sandbox_digest",
            "gpu",
            "priority",
            "status",
            "fingerprint",
            "attempt",
            "timeout",
            "requested_at",
            "started_at",
            "finished_at",
            "duration",
            "exit_code",
            "output",
            "error",
            "runner_pid",
        }
    )

    def create_job(
        self,
        *,
        source: str,
        commit_ref: str,
        commands: list[str],
        wave: int | None = None,
        requester: str | None = None,
        tree: str | None = None,
        sandbox: dict[str, Any] | None = None,
        sandbox_digest: str | None = None,
        gpu: str = "none",
        priority: int = 0,
        fingerprint: str | None = None,
        timeout: int = 3600,
    ) -> int:
        ts = now()
        with self.tx() as c:
            c.execute(
                "INSERT INTO jobs(wave, requester, source, commit_ref, tree, commands,"
                " sandbox, sandbox_digest, gpu, priority, status, fingerprint, attempt,"
                " timeout, requested_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    wave,
                    requester,
                    source,
                    commit_ref,
                    tree,
                    json.dumps(commands),
                    json.dumps(sandbox) if sandbox is not None else None,
                    sandbox_digest,
                    gpu,
                    priority,
                    "queued",
                    fingerprint,
                    0,
                    int(timeout),
                    ts,
                ),
            )
            return int(c.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])

    def get_job(self, job_id: str | int) -> dict[str, Any] | None:
        return _dict(
            self.conn.execute("SELECT * FROM jobs WHERE id=?", (int(job_id),)).fetchone()
        )

    def list_jobs(
        self, *, statuses: Sequence[str] | None = None, limit: int | None = None
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM jobs"
        params: list[Any] = []
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            sql += f" WHERE status IN ({placeholders})"
            params.extend(statuses)
        sql += " ORDER BY priority DESC, requested_at ASC, id ASC"
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        return _dicts(self.conn.execute(sql, params).fetchall())

    def update_job(self, job_id: str | int, **fields: Any) -> None:
        unknown = set(fields) - self.JOB_FIELDS
        if unknown:
            raise SlicemeError(f"unknown job fields: {', '.join(sorted(unknown))}")
        if not fields:
            return
        sets = ", ".join(f"{name}=?" for name in fields)
        params = list(fields.values()) + [int(job_id)]
        self.conn.execute(f"UPDATE jobs SET {sets} WHERE id=?", params)

    def claim_next_job(self, *, runner_pid: int | None = None) -> dict[str, Any] | None:
        """Atomically claim the highest-priority queued job."""
        with self.tx() as c:
            row = c.execute(
                "SELECT * FROM jobs WHERE status='queued'"
                " ORDER BY priority DESC, requested_at ASC, id ASC LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            c.execute(
                "UPDATE jobs SET status='running', started_at=?, runner_pid=? WHERE id=?",
                (now(), runner_pid, int(row["id"])),
            )
            claimed = c.execute("SELECT * FROM jobs WHERE id=?", (int(row["id"]),)).fetchone()
            return _dict(claimed)

    def find_passed_job(self, fingerprint: str) -> dict[str, Any] | None:
        """A passing job for the same fingerprint, for cache/dedupe."""
        return _dict(
            self.conn.execute(
                "SELECT * FROM jobs WHERE fingerprint=? AND status='passed'"
                " ORDER BY id DESC LIMIT 1",
                (fingerprint,),
            ).fetchone()
        )

    def recover_orphan_jobs(self, *, cutoff: float) -> int:
        """Reset ``running`` jobs older than *cutoff* back to ``queued``."""
        with self.tx() as c:
            cursor = c.execute(
                "UPDATE jobs SET status='queued', started_at=NULL, runner_pid=NULL,"
                " error='recovered orphaned lease'"
                " WHERE status='running' AND (started_at IS NULL OR started_at < ?)",
                (cutoff,),
            )
            return int(cursor.rowcount)

    def job_counts(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS c FROM jobs GROUP BY status"
        ).fetchall()
        return {str(r["status"]): int(r["c"]) for r in rows}

    # ---- campaign sessions (resume registry projection) ---------------
    CAMPAIGN_SESSION_FIELDS = frozenset(
        {
            "feature_branch",
            "pi_session_id",
            "session_file",
            "label",
            "status",
            "reason",
            "wave",
            "suspended_at",
        }
    )

    def upsert_campaign_session(
        self, *, feature_branch: str, **fields: Any
    ) -> dict[str, Any]:
        """Insert or update the discovery row for one campaign branch."""
        unknown = set(fields) - self.CAMPAIGN_SESSION_FIELDS
        if unknown:
            raise SlicemeError(
                f"unknown campaign_session fields: {', '.join(sorted(unknown))}"
            )
        ts = now()
        existing = self.get_campaign_session(feature_branch)
        if existing is None:
            columns = ["feature_branch", "created_at", "updated_at", *fields]
            placeholders = ",".join("?" for _ in columns)
            values: list[Any] = [feature_branch, ts, ts, *fields.values()]
            with self.tx() as c:
                c.execute(
                    f"INSERT INTO campaign_sessions({','.join(columns)}) "
                    f"VALUES({placeholders})",
                    values,
                )
                row = c.execute(
                    "SELECT * FROM campaign_sessions WHERE feature_branch=?",
                    (feature_branch,),
                ).fetchone()
                return _dict(row)  # type: ignore[return-value]
        sets = ["updated_at=?"]
        params: list[Any] = [ts]
        for name, value in fields.items():
            sets.append(f"{name}=?")
            params.append(value)
        params.append(int(existing["id"]))
        self.conn.execute(
            f"UPDATE campaign_sessions SET {', '.join(sets)} WHERE id=?", params
        )
        return self.get_campaign_session(feature_branch)  # type: ignore[return-value]

    def get_campaign_session(self, feature_branch: str) -> dict[str, Any] | None:
        return _dict(
            self.conn.execute(
                "SELECT * FROM campaign_sessions WHERE feature_branch=?"
                " ORDER BY id DESC LIMIT 1",
                (feature_branch,),
            ).fetchone()
        )

    def list_campaign_sessions(self) -> list[dict[str, Any]]:
        return _dicts(
            self.conn.execute(
                "SELECT * FROM campaign_sessions ORDER BY updated_at DESC, id DESC"
            ).fetchall()
        )

    def delete_campaign_session(self, feature_branch: str) -> int:
        with self.tx() as c:
            cursor = c.execute(
                "DELETE FROM campaign_sessions WHERE feature_branch=?", (feature_branch,)
            )
            return int(cursor.rowcount)

    # ---- attempts (per-subagent fidelity) ----------------------------
    ATTEMPT_FIELDS = frozenset(
        {
            "node",
            "unit",
            "attempt",
            "agent",
            "status",
            "started_at",
            "finished_at",
            "duration",
            "exit_code",
            "turns",
            "tool_calls",
            "tools",
            "tokens_in",
            "tokens_out",
            "cost",
            "last_tool",
            "last_activity_at",
            "error",
        }
    )

    def create_attempt(
        self,
        *,
        node: str,
        unit: str | None = None,
        attempt: int = 1,
        agent: str = "worker",
        started_at: float | None = None,
    ) -> int:
        ts = now() if started_at is None else float(started_at)
        with self.tx() as c:
            c.execute(
                "INSERT INTO attempts(node, unit, attempt, agent, status, started_at,"
                " last_activity_at) VALUES(?,?,?,?,?,?,?)",
                (node, unit, int(attempt), agent, "running", ts, ts),
            )
            return int(c.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])

    def finish_attempt(self, attempt_id: int, **fields: Any) -> dict[str, Any] | None:
        unknown = set(fields) - self.ATTEMPT_FIELDS
        if unknown:
            raise SlicemeError(f"unknown attempt fields: {', '.join(sorted(unknown))}")
        ts = now()
        row = _dict(
            self.conn.execute("SELECT * FROM attempts WHERE id=?", (int(attempt_id),)).fetchone()
        )
        if row is None:
            return None
        fields.setdefault("status", "ok")
        fields.setdefault("finished_at", ts)
        started = float(row.get("started_at") or ts)
        fields.setdefault("duration", ts - started)
        sets = ", ".join(f"{name}=?" for name in fields)
        params = list(fields.values()) + [int(attempt_id)]
        self.conn.execute(f"UPDATE attempts SET {sets} WHERE id=?", params)
        return _dict(
            self.conn.execute("SELECT * FROM attempts WHERE id=?", (int(attempt_id),)).fetchone()
        )

    def get_attempt(self, attempt_id: str | int) -> dict[str, Any] | None:
        return _dict(
            self.conn.execute("SELECT * FROM attempts WHERE id=?", (int(attempt_id),)).fetchone()
        )

    def list_attempts(
        self, *, node: str | None = None, statuses: Sequence[str] | None = None
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM attempts"
        clauses: list[str] = []
        params: list[Any] = []
        if node:
            clauses.append("node=?")
            params.append(node)
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            clauses.append(f"status IN ({placeholders})")
            params.extend(statuses)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id ASC"
        return _dicts(self.conn.execute(sql, params).fetchall())

    def latest_attempt(self, node: str) -> dict[str, Any] | None:
        return _dict(
            self.conn.execute(
                "SELECT * FROM attempts WHERE node=? ORDER BY id DESC LIMIT 1", (node,)
            ).fetchone()
        )

    def find_running_attempt(
        self, node: str, attempt: int | None = None
    ) -> dict[str, Any] | None:
        sql = "SELECT * FROM attempts WHERE node=? AND status='running'"
        params: list[Any] = [node]
        if attempt is not None:
            sql += " AND attempt=?"
            params.append(int(attempt))
        sql += " ORDER BY id DESC LIMIT 1"
        return _dict(self.conn.execute(sql, params).fetchone())
