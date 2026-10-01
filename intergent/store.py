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

from .util import IntergentError, db_path, now

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
        self.conn.commit()

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
    ) -> int:
        ts = now()
        with self.tx() as c:
            c.execute(
                "INSERT INTO candidates(unit_id, branch, head_commit, base_commit,"
                " priority, status, summary, created_at, updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (unit_id, branch, head_commit, base_commit, priority, "prepared", summary, ts, ts),
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
                " WHERE u.name=? ORDER BY c.id DESC LIMIT 1",
                (text,),
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
            raise IntergentError(f"unknown unit: {name_or_id}")
        return unit
