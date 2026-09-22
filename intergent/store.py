"""SQLite store for the local plane (WAL).

Mirrors the sketch in ``docs/operations.md`` (local store: SQLite/WAL).  The
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

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS agents (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE NOT NULL,
  model TEXT,
  parent_id INTEGER REFERENCES agents(id),
  created_at REAL NOT NULL
);

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
  agent_id INTEGER REFERENCES agents(id),
  state TEXT NOT NULL DEFAULT 'working',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  UNIQUE(session_id, name)
);

CREATE TABLE IF NOT EXISTS scopes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  key TEXT NOT NULL,
  canonical TEXT NOT NULL,
  node TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS intents (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  unit_id INTEGER NOT NULL REFERENCES units(id),
  task TEXT,
  summary TEXT,
  operation TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS intent_scopes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  intent_id INTEGER NOT NULL REFERENCES intents(id),
  scope_id INTEGER NOT NULL REFERENCES scopes(id),
  operation TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'declared'
);

CREATE TABLE IF NOT EXISTS lock_requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  intent_id INTEGER NOT NULL REFERENCES intents(id),
  unit_id INTEGER NOT NULL REFERENCES units(id),
  status TEXT NOT NULL,
  requirements TEXT NOT NULL,
  blocker_unit_id INTEGER,
  position INTEGER,
  reason TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS claims (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id INTEGER NOT NULL REFERENCES lock_requests(id),
  intent_id INTEGER NOT NULL REFERENCES intents(id),
  unit_id INTEGER NOT NULL REFERENCES units(id),
  node TEXT NOT NULL,
  mode TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'granted',
  ttl_seconds INTEGER NOT NULL,
  heartbeat_at REAL NOT NULL,
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS dependencies (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  intent_id INTEGER NOT NULL REFERENCES intents(id),
  depends_on_intent_id INTEGER NOT NULL REFERENCES intents(id),
  kind TEXT NOT NULL DEFAULT 'lease_order',
  created_at REAL NOT NULL,
  UNIQUE(intent_id, depends_on_intent_id, kind)
);

CREATE TABLE IF NOT EXISTS candidates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  unit_id INTEGER NOT NULL REFERENCES units(id),
  intent_id INTEGER REFERENCES intents(id),
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
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  intent_id INTEGER NOT NULL REFERENCES intents(id),
  related_intent_id INTEGER,
  verdict TEXT NOT NULL,
  severity TEXT,
  rationale TEXT,
  action TEXT,
  reason TEXT,
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  unit_id INTEGER,
  intent_id INTEGER,
  candidate_id INTEGER,
  data TEXT,
  created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_claims_node ON claims(node, state);
CREATE INDEX IF NOT EXISTS idx_claims_unit ON claims(unit_id, state);
CREATE INDEX IF NOT EXISTS idx_intents_unit ON intents(unit_id, status);
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
        self._migrate_states()
        self.conn.commit()

    def _migrate_states(self) -> None:
        """Collapse pre-handoff/post-handoff states onto the merged model.

        Legacy databases used ``active``/``finished`` units and
        ``ready``/``verified``/``approved`` candidates; the handoff model keeps
        ``working`` units and ``prepared`` candidates.
        """
        self.conn.execute(
            "UPDATE units SET state='working' WHERE state IN ('active','finished')"
        )
        self.conn.execute(
            "UPDATE units SET state='closed' WHERE state IN ('released','abandoned')"
        )
        self.conn.execute(
            "UPDATE candidates SET status='prepared' "
            "WHERE status IN ('ready','verified','approved','blocked','failed')"
        )

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

    # ---- meta ---------------------------------------------------------
    def set_meta(self, key: str, value: Any) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )

    def get_meta(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except (TypeError, json.JSONDecodeError):
            return row["value"]

    # ---- events -------------------------------------------------------
    def event(
        self,
        kind: str,
        *,
        unit_id: int | None = None,
        intent_id: int | None = None,
        candidate_id: int | None = None,
        data: Any = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO events(kind, unit_id, intent_id, candidate_id, data, created_at)"
            " VALUES(?,?,?,?,?,?)",
            (kind, unit_id, intent_id, candidate_id, json.dumps(data) if data else None, now()),
        )

    def recent_events(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return _dicts(rows)

    # ---- agents -------------------------------------------------------
    def upsert_agent(self, name: str, model: str | None, parent_id: int | None) -> int:
        with self.tx() as c:
            c.execute(
                "INSERT INTO agents(name, model, parent_id, created_at) VALUES(?,?,?,?)"
                " ON CONFLICT(name) DO UPDATE SET model=excluded.model,"
                " parent_id=excluded.parent_id",
                (name, model, parent_id, now()),
            )
            row = c.execute("SELECT id FROM agents WHERE name=?", (name,)).fetchone()
        return int(row["id"])

    def get_agent(self, name: str) -> dict[str, Any] | None:
        return _dict(
            self.conn.execute("SELECT * FROM agents WHERE name=?", (name,)).fetchone()
        )

    def list_agents(self) -> list[dict[str, Any]]:
        return _dicts(self.conn.execute("SELECT * FROM agents ORDER BY id").fetchall())

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

    def list_sessions(self) -> list[dict[str, Any]]:
        return _dicts(self.conn.execute("SELECT * FROM sessions ORDER BY id").fetchall())

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
        agent_id: int | None,
    ) -> int:
        ts = now()
        with self.tx() as c:
            c.execute(
                "INSERT INTO units(session_id, name, kind, worktree, branch, base_commit,"
                " agent_id, state, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (session_id, name, kind, worktree, branch, base_commit, agent_id, "working", ts, ts),
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

    # ---- scopes -------------------------------------------------------
    def get_or_create_scope(self, kind: str, key: str, canonical: str) -> int:
        node = f"{kind}:{canonical}"
        row = self.conn.execute("SELECT id FROM scopes WHERE node=?", (node,)).fetchone()
        if row is not None:
            return int(row["id"])
        with self.tx() as c:
            c.execute(
                "INSERT INTO scopes(kind, key, canonical, node) VALUES(?,?,?,?)",
                (kind, key, canonical, node),
            )
            row = c.execute("SELECT id FROM scopes WHERE node=?", (node,)).fetchone()
        return int(row["id"])

    def scope_by_node(self, node: str) -> dict[str, Any] | None:
        return _dict(self.conn.execute("SELECT * FROM scopes WHERE node=?", (node,)).fetchone())

    # ---- intents ------------------------------------------------------
    def create_intent(
        self, unit_id: int, task: str | None, summary: str | None, operation: str
    ) -> int:
        ts = now()
        with self.tx() as c:
            c.execute(
                "INSERT INTO intents(unit_id, task, summary, operation, status, created_at,"
                " updated_at) VALUES(?,?,?,?,?,?,?)",
                (unit_id, task, summary, operation, "active", ts, ts),
            )
            return int(c.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])

    def add_intent_scope(
        self, intent_id: int, scope_id: int, operation: str, source: str = "declared"
    ) -> None:
        self.conn.execute(
            "INSERT INTO intent_scopes(intent_id, scope_id, operation, source) VALUES(?,?,?,?)",
            (intent_id, scope_id, operation, source),
        )

    def set_intent_status(self, intent_id: int, status: str) -> None:
        self.conn.execute(
            "UPDATE intents SET status=?, updated_at=? WHERE id=?", (status, now(), intent_id)
        )

    def get_intent(self, intent_id: int) -> dict[str, Any] | None:
        return _dict(
            self.conn.execute("SELECT * FROM intents WHERE id=?", (intent_id,)).fetchone()
        )

    def intent_scopes(self, intent_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT i.operation, i.source, s.kind, s.key, s.canonical, s.node"
            " FROM intent_scopes i JOIN scopes s ON s.id = i.scope_id"
            " WHERE i.intent_id=? ORDER BY s.node",
            (intent_id,),
        ).fetchall()
        return _dicts(rows)

    def active_intents(self, *, exclude_unit: int | None = None) -> list[dict[str, Any]]:
        sql = (
            "SELECT i.*, u.name AS unit_name FROM intents i"
            " JOIN units u ON u.id = i.unit_id"
            " WHERE i.status IN ('granted','queued','needs_decision','active')"
        )
        params: list[Any] = []
        if exclude_unit is not None:
            sql += " AND i.unit_id != ?"
            params.append(exclude_unit)
        rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = _dict(row)
            item["scopes"] = self.intent_scopes(int(row["id"]))
            result.append(item)
        return result

    def latest_intent_for_unit(self, unit_id: int) -> dict[str, Any] | None:
        return _dict(
            self.conn.execute(
                "SELECT * FROM intents WHERE unit_id=? ORDER BY id DESC LIMIT 1", (unit_id,)
            ).fetchone()
        )

    # ---- lock requests / claims --------------------------------------
    def create_lock_request(
        self,
        *,
        intent_id: int,
        unit_id: int,
        status: str,
        requirements: dict[str, str],
        blocker_unit_id: int | None = None,
        position: int | None = None,
        reason: str | None = None,
    ) -> int:
        ts = now()
        with self.tx() as c:
            c.execute(
                "INSERT INTO lock_requests(intent_id, unit_id, status, requirements,"
                " blocker_unit_id, position, reason, created_at, updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    intent_id,
                    unit_id,
                    status,
                    json.dumps(requirements, sort_keys=True),
                    blocker_unit_id,
                    position,
                    reason,
                    ts,
                    ts,
                ),
            )
            return int(c.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])

    def set_lock_request_status(
        self,
        request_id: int,
        status: str,
        *,
        blocker_unit_id: int | None = None,
        reason: str | None = None,
    ) -> None:
        self.conn.execute(
            "UPDATE lock_requests SET status=?, blocker_unit_id=?, reason=?, updated_at=?"
            " WHERE id=?",
            (status, blocker_unit_id, reason, now(), request_id),
        )

    def grant_claims(
        self,
        *,
        request_id: int,
        intent_id: int,
        unit_id: int,
        requirements: dict[str, str],
        ttl_seconds: int,
    ) -> None:
        ts = now()
        with self.tx() as c:
            for node, mode in requirements.items():
                c.execute(
                    "INSERT INTO claims(request_id, intent_id, unit_id, node, mode, state,"
                    " ttl_seconds, heartbeat_at, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (request_id, intent_id, unit_id, node, mode, "granted", ttl_seconds, ts, ts),
                )

    def release_claims(self, unit_id: int, *, state: str = "released") -> None:
        self.conn.execute(
            "UPDATE claims SET state=? WHERE unit_id=? AND state='granted'", (state, unit_id)
        )

    def held_claims(self, *, exclude_unit: int | None = None) -> list[dict[str, Any]]:
        sql = (
            "SELECT c.*, u.name AS unit_name FROM claims c"
            " JOIN units u ON u.id = c.unit_id WHERE c.state='granted'"
        )
        params: list[Any] = []
        if exclude_unit is not None:
            sql += " AND c.unit_id != ?"
            params.append(exclude_unit)
        rows = self.conn.execute(sql, params).fetchall()
        return _dicts(rows)

    def get_lock_request_for_unit(self, unit_id: int) -> dict[str, Any] | None:
        return _dict(
            self.conn.execute(
                "SELECT * FROM lock_requests WHERE unit_id=? AND status IN"
                " ('granted','queued','needs_decision') ORDER BY id DESC LIMIT 1",
                (unit_id,),
            ).fetchone()
        )

    def queued_requests(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT lr.*, u.name AS unit_name FROM lock_requests lr"
            " JOIN units u ON u.id = lr.unit_id"
            " WHERE lr.status='queued' ORDER BY lr.created_at ASC, lr.id ASC"
        ).fetchall()
        return _dicts(rows)

    def expire_claims(self, now_ts: float) -> list[int]:
        """Return unit ids whose granted claims have gone stale."""
        rows = self.conn.execute(
            "SELECT DISTINCT unit_id FROM claims WHERE state='granted'"
            " AND (heartbeat_at + ttl_seconds) < ?",
            (now_ts,),
        ).fetchall()
        return [int(r["unit_id"]) for r in rows]

    def heartbeat(self, unit_id: int, ttl_seconds: int) -> int:
        cur = self.conn.execute(
            "UPDATE claims SET heartbeat_at=?, ttl_seconds=?"
            " WHERE unit_id=? AND state='granted'",
            (now(), ttl_seconds, unit_id),
        )
        return cur.rowcount

    def queue_position(self, request_id: int) -> int:
        row = self.conn.execute(
            "SELECT id FROM lock_requests WHERE status='queued' ORDER BY created_at ASC, id ASC"
        ).fetchall()
        for index, r in enumerate(row, start=1):
            if int(r["id"]) == request_id:
                return index
        return 0

    # ---- dependencies -------------------------------------------------
    def add_dependency(self, intent_id: int, depends_on: int, kind: str = "lease_order") -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO dependencies(intent_id, depends_on_intent_id, kind, created_at)"
            " VALUES(?,?,?,?)",
            (intent_id, depends_on, kind, now()),
        )

    def dependencies_for_intent(self, intent_id: int) -> list[dict[str, Any]]:
        return _dicts(
            self.conn.execute(
                "SELECT * FROM dependencies WHERE intent_id=?", (intent_id,)
            ).fetchall()
        )

    # ---- candidates ---------------------------------------------------
    def create_candidate(
        self,
        *,
        unit_id: int,
        intent_id: int | None,
        branch: str,
        head_commit: str,
        base_commit: str,
        priority: int,
        summary: str | None,
    ) -> int:
        ts = now()
        with self.tx() as c:
            c.execute(
                "INSERT INTO candidates(unit_id, intent_id, branch, head_commit, base_commit,"
                " priority, status, summary, created_at, updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (unit_id, intent_id, branch, head_commit, base_commit, priority, "prepared", summary, ts, ts),
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
                " toolchain_digest, policy_digest, created_at) VALUES(?,?,?,?,?,?,?)",
                (candidate_id, fingerprint, tree, cmd_digest, toolchain_digest, policy_digest, now()),
            )
            return int(c.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])

    def add_verification(
        self,
        candidate_id: int,
        fingerprint_id: int,
        status: str,
        output: str,
        duration: float,
    ) -> int:
        with self.tx() as c:
            c.execute(
                "INSERT INTO verifications(candidate_id, fingerprint_id, status, output,"
                " duration, created_at) VALUES(?,?,?,?,?,?)",
                (candidate_id, fingerprint_id, status, output, duration, now()),
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
                "SELECT * FROM verifications WHERE candidate_id=? ORDER BY id DESC LIMIT 1",
                (candidate_id,),
            ).fetchone()
        )

    # ---- decisions ----------------------------------------------------
    def add_decision(
        self,
        *,
        intent_id: int,
        related_intent_id: int | None,
        verdict: str,
        severity: str | None,
        rationale: str | None,
        action: str | None,
        reason: str | None,
    ) -> int:
        with self.tx() as c:
            c.execute(
                "INSERT INTO decisions(intent_id, related_intent_id, verdict, severity,"
                " rationale, action, reason, created_at) VALUES(?,?,?,?,?,?,?,?)",
                (intent_id, related_intent_id, verdict, severity, rationale, action, reason, now()),
            )
            return int(c.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])

    def list_decisions(self, intent_id: int | None = None) -> list[dict[str, Any]]:
        if intent_id is None:
            rows = self.conn.execute("SELECT * FROM decisions ORDER BY id DESC").fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM decisions WHERE intent_id=? ORDER BY id DESC", (intent_id,)
            ).fetchall()
        return _dicts(rows)

    def require_unit(self, name_or_id: str | int) -> dict[str, Any]:
        unit = self.get_unit(name_or_id)
        if unit is None:
            raise IntergentError(f"unknown unit: {name_or_id}")
        return unit
