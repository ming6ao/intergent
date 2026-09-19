"""Service layer: the single owner of local-plane state.

Every adapter (CLI, MCP, supervisor) calls these functions.  This mirrors the
"one engine, many adapters / no adapter owns state" rule in
``docs/architecture.md``.  Business rules live here; persistence lives in
``store``; git mutation lives in ``gitutil``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from . import conflict, gitutil, landing, locks, planner
from .conflict import Finding, IntentRef
from .locks import HeldLock, Requirement
from .scopes import (
    classify_operation,
    make_scope,
    parse_scope_specs,
)
from .store import Store
from .util import (
    IntergentError,
    config_path,
    now,
    read_json,
    slugify,
    worktrees_dir,
    write_json,
)
from .verifier import (
    compute_fingerprint,
    run_checks,
)

DEFAULT_ATTACHMENT = "terminal"


class Service:
    def __init__(self, root: Path, store: Store | None = None):
        self.root = root
        self.store = store or Store(root)

    def close(self) -> None:
        self.store.close()

    @property
    def config(self) -> dict[str, Any]:
        cfg = read_json(config_path(self.root))
        if cfg is None:
            raise IntergentError("missing .intergent/config.json")
        return cfg

    # ------------------------------------------------------------------
    # Bootstrap
    # ------------------------------------------------------------------
    @classmethod
    def init_plane(
        cls,
        root: Path,
        *,
        main_branch: str | None = None,
        base: str | None = None,
        checks: list[dict[str, Any]] | None = None,
        lease_ttl_seconds: int = 1800,
        force: bool = False,
    ) -> dict[str, Any]:
        """Create the on-disk plane (config + state db) for a git repo."""
        from .util import ensure_parent, state_dir

        if not gitutil.is_git_repo(root):
            raise IntergentError(f"{root} is not a git repository")
        state = state_dir(root)
        cfg_file = config_path(root)
        if cfg_file.exists() and not force:
            raise IntergentError(
                "already initialised (.intergent/config.json exists); use --force to reset config"
            )
        detected = gitutil.current_branch(root) or "main"
        main_branch = main_branch or detected
        if not gitutil.branch_exists(root, main_branch):
            # An empty repository with no commits yet: create the branch lazily.
            main_branch = detected
        base = base or main_branch
        config = {
            "version": 1,
            "main_branch": main_branch,
            "base": base,
            "lease_ttl_seconds": lease_ttl_seconds,
            "checks": checks or [],
            "policy": {"require_verification": True, "allow_auto_approve": []},
            "created_at": now(),
        }
        ensure_parent(cfg_file)
        write_json(cfg_file, config)
        state.mkdir(parents=True, exist_ok=True)
        store = Store(root)
        store.set_meta("version", 1)
        store.set_meta("created_at", now())
        store.close()
        _ensure_gitignore(root)
        return config

    @classmethod
    def init(
        cls,
        path: str | os.PathLike[str] | None = None,
        *,
        name: str | None = None,
        agent: str | None = None,
        session: str | None = None,
        base: str | None = None,
        kind: str = "worker",
        main_branch: str | None = None,
        checks: list[dict[str, Any]] | None = None,
        lease_ttl_seconds: int = 1800,
        force: bool = False,
    ) -> dict[str, Any]:
        """Bootstrap the plane and a unit for *path* (default cwd), idempotently.

        Safe to call on every session start:

        1. If no plane is found walking up from *path*, create it at the git root.
        2. If *path* is not already inside a unit worktree, create one.

        Returns a summary including ``worktree`` so the caller can bind its
        tools to the unit (the running process cwd is not changed).
        """
        start = Path(path or os.getcwd()).resolve()

        root: Path | None = None
        for candidate in [start, *start.parents]:
            if config_path(candidate).is_file():
                root = candidate
                break

        initialized = False
        if root is None or force:
            if not gitutil.is_git_repo(start):
                raise IntergentError(f"{start} is not a git repository")
            root = gitutil.toplevel(start)
            cls.init_plane(
                root,
                main_branch=main_branch,
                base=base,
                checks=checks,
                lease_ttl_seconds=lease_ttl_seconds,
                force=force,
            )
            initialized = True

        # Keep the exclude entry fresh even when the plane already existed and
        # the repo's .git/info/exclude was reset (e.g. re-cloned metadata).
        _ensure_gitignore(root)

        service = cls(root)
        try:
            unit = service.current_unit(start)
            created = False
        except IntergentError:
            existing = {u["name"] for u in service.list_units()}
            base_name = name or slugify(start.name) or "session"
            unit_name = base_name
            counter = 2
            while unit_name in existing:
                unit_name = f"{base_name}-{counter}"
                counter += 1
            unit = service.create_workspace(
                unit_name,
                session=session,
                kind=kind,
                base=base,
                agent=agent,
            )
            created = True
        finally:
            service.close()

        return {
            "root": str(root),
            "initialized": initialized,
            "created": created,
            "unit": unit["name"],
            "branch": unit.get("branch"),
            "worktree": unit.get("worktree"),
        }

    # ------------------------------------------------------------------
    # Sessions / units
    # ------------------------------------------------------------------
    def create_session(
        self, name: str, *, task: str | None = None, attachment: str = DEFAULT_ATTACHMENT
    ) -> dict[str, Any]:
        existing = self.store.get_session(name)
        if existing is not None:
            return existing
        session_id = self.store.create_session(name, task, attachment)
        self.store.conn.commit()
        self.store.event("session.created", data={"name": name, "task": task})
        return self.store.get_session(session_id)  # type: ignore[return-value]

    def create_workspace(
        self,
        name: str,
        *,
        session: str | None = None,
        kind: str = "worker",
        base: str | None = None,
        agent: str | None = None,
        task: str | None = None,
    ) -> dict[str, Any]:
        config = self.config
        session_name = session or name
        sess = self.store.get_session(session_name)
        if sess is None:
            sess = self.create_session(session_name, task=task)
        session_id = int(sess["id"])

        base_ref = base or config.get("base") or config.get("main_branch") or "main"
        base_commit = gitutil.rev_parse(self.root, base_ref)

        agent_id = None
        if agent:
            record = self.store.get_agent(agent)
            if record is None:
                agent_id = self.store.upsert_agent(agent, None, None)
            else:
                agent_id = int(record["id"])

        branch = _unique_branch(self.root, session_name, name)
        worktree = _unique_worktree(worktrees_dir(self.root), session_name, name)
        gitutil.add_worktree(self.root, worktree, branch=branch, base=base_commit)
        try:
            unit_id = self.store.create_unit(
                session_id=session_id,
                name=name,
                kind=kind,
                worktree=str(worktree),
                branch=branch,
                base_commit=base_commit,
                agent_id=agent_id,
            )
        except Exception:
            gitutil.remove_worktree(self.root, worktree, force=True)
            raise
        self.store.conn.commit()
        self.store.event("unit.created", unit_id=unit_id, data={"branch": branch, "kind": kind})
        return self.store.get_unit(unit_id)  # type: ignore[return-value]

    def list_units(self) -> list[dict[str, Any]]:
        units = self.store.list_units()
        for unit in units:
            intent = self.store.latest_intent_for_unit(int(unit["id"]))
            unit["intent"] = intent
            request = self.store.get_lock_request_for_unit(int(unit["id"]))
            unit["lock_request"] = request
        return units

    def unit_detail(self, unit_ref: str | int) -> dict[str, Any]:
        unit = self.store.require_unit(unit_ref)
        unit["intent"] = self.store.latest_intent_for_unit(int(unit["id"]))
        unit["lock_request"] = self.store.get_lock_request_for_unit(int(unit["id"]))
        unit["candidates"] = [
            c for c in self.store.list_candidates() if int(c["unit_id"]) == int(unit["id"])
        ]
        return unit

    def current_unit(self, path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
        """Resolve the unit whose worktree contains *path* (default cwd).

        Lets an agent launched inside its own worktree call the CLI/MCP without
        passing ``--unit``. Falls back to matching the checked-out branch.
        """
        resolved = Path(path or os.getcwd()).resolve()
        best: dict[str, Any] | None = None
        best_len = -1
        for unit in self.store.list_units():
            try:
                worktree = Path(unit["worktree"]).resolve()
            except (OSError, TypeError):
                continue
            if worktree == resolved or worktree in resolved.parents:
                if len(str(worktree)) > best_len:
                    best, best_len = unit, len(str(worktree))
        if best is not None:
            return best
        branch = gitutil.current_branch(resolved)
        if branch:
            for unit in self.store.list_units():
                if unit["branch"] == branch:
                    return unit
        raise IntergentError(
            "current directory is not an Intergent unit worktree; "
            "run `intergent workspace create` and cd into it, or pass --unit"
        )

    # ------------------------------------------------------------------
    # Intent / conflict / leases
    # ------------------------------------------------------------------
    def _intent_ref(self, row: dict[str, Any]) -> IntentRef:
        return IntentRef(
            intent_id=int(row["id"]),
            unit_id=int(row["unit_id"]),
            unit_name=row.get("unit_name") or f"unit-{row['unit_id']}",
            operation=row["operation"],
            scopes=[(make_scope(s["kind"], s["key"]), s["operation"]) for s in row.get("scopes", [])],
        )

    def _other_refs(self, unit_id: int) -> list[IntentRef]:
        return [self._intent_ref(row) for row in self.store.active_intents(exclude_unit=unit_id)]

    def check_conflicts(
        self,
        unit_ref: str | int,
        operation: str,
        scope_specs: list[str],
    ) -> list[Finding]:
        unit = self.store.require_unit(unit_ref)
        items = parse_scope_specs(scope_specs, operation)
        ref = IntentRef(
            intent_id=0,
            unit_id=int(unit["id"]),
            unit_name=unit["name"],
            operation=operation,
            scopes=items,
        )
        return conflict.evaluate(ref, self._other_refs(int(unit["id"])))

    def declare_intent(
        self,
        unit_ref: str | int,
        *,
        operation: str,
        scope_specs: list[str],
        task: str | None = None,
        summary: str | None = None,
    ) -> dict[str, Any]:
        self._reap_expired()
        unit = self.store.require_unit(unit_ref)
        if unit["state"] != "active":
            raise IntergentError(f"unit {unit['name']} is {unit['state']}, not active")
        items = parse_scope_specs(scope_specs, operation)
        operation = classify_operation(operation)

        # Supersede a previous in-flight intent on this unit.
        previous = self.store.get_lock_request_for_unit(int(unit["id"]))
        if previous is not None:
            self._release_unit(int(unit["id"]), status="released")
            self.store.set_intent_status(int(previous["intent_id"]), "superseded")

        findings = self.check_conflicts(unit_ref, operation, scope_specs)
        intent_id = self.store.create_intent(
            int(unit["id"]), task, summary, operation
        )
        for scope, op in items:
            scope_id = self.store.get_or_create_scope(scope.kind, scope.key, scope.canonical)
            self.store.add_intent_scope(intent_id, scope_id, op)
        self.store.conn.commit()

        requirements = locks.requirement_closure(items)
        req_map = {node: req.mode for node, req in requirements.items()}
        ttl = int(self.config.get("lease_ttl_seconds", 1800))

        if conflict.requires_decision(findings):
            blocker = self._blocker_from_findings(findings)
            request_id = self.store.create_lock_request(
                intent_id=intent_id,
                unit_id=int(unit["id"]),
                status="needs_decision",
                requirements=req_map,
                blocker_unit_id=blocker,
                reason="destructive vs additive requires an explicit decision",
            )
            self.store.set_intent_status(intent_id, "needs_decision")
            self.store.add_decision(
                intent_id=intent_id,
                related_intent_id=_finding_intent(findings),
                verdict="FM-C001 destructive_vs_additive",
                severity="HIGH",
                rationale="destructive and additive declarations overlap on an exact scope",
                action=None,
                reason=None,
            )
            self.store.conn.commit()
            self._log("declare", unit, intent_id, {"status": "needs_decision"})
            return {
                "intent_id": intent_id,
                "status": "needs_decision",
                "requirements": req_map,
                "findings": [f.to_dict() for f in findings],
                "request_id": request_id,
                "blocker": blocker,
                "options": ["wait", "redesign", "override"],
            }

        held = [
            HeldLock(
                unit_id=int(c["unit_id"]),
                unit_name=c["unit_name"],
                node=c["node"],
                mode=c["mode"],
            )
            for c in self.store.held_claims(exclude_unit=int(unit["id"]))
        ]
        blocker_lock = locks.find_blocker(requirements, held)
        if blocker_lock is None:
            request_id = self.store.create_lock_request(
                intent_id=intent_id,
                unit_id=int(unit["id"]),
                status="granted",
                requirements=req_map,
            )
            self.store.grant_claims(
                request_id=request_id,
                intent_id=intent_id,
                unit_id=int(unit["id"]),
                requirements=req_map,
                ttl_seconds=ttl,
            )
            self.store.set_intent_status(intent_id, "granted")
            self.store.conn.commit()
            self._log("declare", unit, intent_id, {"status": "granted"})
            return {
                "intent_id": intent_id,
                "status": "granted",
                "requirements": req_map,
                "findings": [f.to_dict() for f in findings],
                "request_id": request_id,
            }

        # Queue behind the blocker.
        blocker_intent = self.store.latest_intent_for_unit(blocker_lock.unit_id)
        if blocker_intent is not None:
            self.store.add_dependency(intent_id, int(blocker_intent["id"]), "lease_order")
        request_id = self.store.create_lock_request(
            intent_id=intent_id,
            unit_id=int(unit["id"]),
            status="queued",
            requirements=req_map,
            blocker_unit_id=blocker_lock.unit_id,
            reason=f"waiting on {blocker_lock.unit_name} ({blocker_lock.node}={blocker_lock.mode})",
        )
        self.store.set_intent_status(intent_id, "queued")
        self.store.conn.commit()
        position = self.store.queue_position(request_id)
        self._log("declare", unit, intent_id, {"status": "queued", "blocker": blocker_lock.unit_name})
        return {
            "intent_id": intent_id,
            "status": "queued",
            "requirements": req_map,
            "findings": [f.to_dict() for f in findings],
            "request_id": request_id,
            "position": position,
            "blocker": blocker_lock.unit_name,
            "blocker_node": blocker_lock.node,
            "eta_seconds": ttl,
        }

    def decide(
        self, intent_id: int, action: str, *, reason: str | None = None
    ) -> dict[str, Any]:
        self._reap_expired()
        intent = self.store.get_intent(int(intent_id))
        if intent is None:
            raise IntergentError(f"unknown intent {intent_id}")
        request = self.store.get_lock_request_for_unit(int(intent["unit_id"]))
        if request is None or int(request["intent_id"]) != int(intent_id):
            raise IntergentError(f"intent {intent_id} has no pending lock request")
        if request["status"] != "needs_decision":
            raise IntergentError(
                f"intent {intent_id} is {request['status']}, not awaiting a decision"
            )
        requirements = {k: str(v) for k, v in _loads(request["requirements"]).items()}
        unit_id = int(intent["unit_id"])
        ttl = int(self.config.get("lease_ttl_seconds", 1800))
        action = action.strip().lower()
        if action not in {"wait", "override", "redesign"}:
            raise IntergentError("action must be one of: wait, override, redesign")

        if action == "redesign":
            self.store.set_lock_request_status(request["id"], "cancelled", reason=reason)
            self.store.set_intent_status(int(intent_id), "superseded")
            self.store.add_decision(
                intent_id=int(intent_id),
                related_intent_id=None,
                verdict="FM-C001 destructive_vs_additive",
                severity="HIGH",
                rationale="user chose to redesign",
                action="redesign",
                reason=reason,
            )
            self.store.conn.commit()
            return {"intent_id": intent_id, "status": "redesign"}

        if action == "override":
            self.store.set_lock_request_status(
                request["id"], "granted", reason=f"override: {reason or 'audited'}"
            )
            self.store.grant_claims(
                request_id=int(request["id"]),
                intent_id=int(intent_id),
                unit_id=unit_id,
                requirements=requirements,
                ttl_seconds=ttl,
            )
            self.store.set_intent_status(int(intent_id), "granted")
            self.store.add_decision(
                intent_id=int(intent_id),
                related_intent_id=None,
                verdict="FM-C001 destructive_vs_additive",
                severity="HIGH",
                rationale="user overrode a HIGH conflict",
                action="override",
                reason=reason,
            )
            self.store.conn.commit()
            return {"intent_id": intent_id, "status": "granted", "overridden": True}

        # wait: queue behind the recorded blocker, then try promotion.
        blocker_unit_id = request["blocker_unit_id"]
        self.store.set_lock_request_status(
            request["id"], "queued", blocker_unit_id=blocker_unit_id, reason="user chose to wait"
        )
        self.store.set_intent_status(int(intent_id), "queued")
        self.store.add_decision(
            intent_id=int(intent_id),
            related_intent_id=None,
            verdict="FM-C001 destructive_vs_additive",
            severity="HIGH",
            rationale="user chose to wait",
            action="wait",
            reason=reason,
        )
        self.store.conn.commit()
        promoted = self._promote_queue()
        request = self.store.get_lock_request_for_unit(unit_id)
        return {
            "intent_id": intent_id,
            "status": request["status"] if request else "queued",
            "promoted": [p["unit_name"] for p in promoted],
        }

    def heartbeat(self, unit_ref: str | int) -> dict[str, Any]:
        self._reap_expired()
        unit = self.store.require_unit(unit_ref)
        ttl = int(self.config.get("lease_ttl_seconds", 1800))
        count = self.store.heartbeat(int(unit["id"]), ttl)
        self.store.conn.commit()
        return {"unit": unit["name"], "renewed": count, "ttl_seconds": ttl}

    def release(self, unit_ref: str | int) -> dict[str, Any]:
        self._reap_expired()
        unit = self.store.require_unit(unit_ref)
        promoted = self._release_unit(int(unit["id"]), status="released")
        return {"unit": unit["name"], "promoted": [p["unit_name"] for p in promoted]}

    # ------------------------------------------------------------------
    # Work / candidates / verification
    # ------------------------------------------------------------------
    def commit(self, unit_ref: str | int, message: str) -> dict[str, Any]:
        unit = self.store.require_unit(unit_ref)
        worktree = Path(unit["worktree"])
        if not worktree.exists():
            raise IntergentError(f"worktree missing: {worktree}")
        result = gitutil.commit_all(worktree, message)
        if not result.ok:
            raise IntergentError(result.stderr.strip() or result.stdout.strip() or "git commit failed")
        return {"unit": unit["name"], "commit": gitutil.head_commit(worktree)}

    def finish(
        self, unit_ref: str | int, *, summary: str | None = None
    ) -> dict[str, Any]:
        unit = self.store.require_unit(unit_ref)
        worktree = Path(unit["worktree"])
        if not gitutil.is_clean(worktree):
            raise IntergentError(
                "worktree has uncommitted changes; commit them (`intergent commit`) before finishing"
            )
        head = gitutil.head_commit(worktree)
        intent = self.store.latest_intent_for_unit(int(unit["id"]))
        existing = self.store.list_candidates()
        candidate = next(
            (c for c in existing if int(c["unit_id"]) == int(unit["id"]) and c["status"] in {"ready", "verified", "failed", "blocked"}),
            None,
        )
        if candidate is not None:
            self.store.update_candidate(int(candidate["id"]), head_commit=head, status="ready")
            cid = int(candidate["id"])
        else:
            cid = self.store.create_candidate(
                unit_id=int(unit["id"]),
                intent_id=int(intent["id"]) if intent else None,
                branch=unit["branch"],
                head_commit=head,
                base_commit=unit.get("base_commit") or "",
                priority=0,
                summary=summary,
            )
        self.store.set_unit_state(int(unit["id"]), "finished")
        self.store.conn.commit()
        self.store.event("candidate.ready", unit_id=int(unit["id"]), candidate_id=cid)
        return self.store.get_candidate(cid)  # type: ignore[return-value]

    def verify(
        self, candidate_ref: str | int, *, force: bool = False
    ) -> dict[str, Any]:
        self._reap_expired()
        candidate = self.store.get_candidate(candidate_ref)
        if candidate is None:
            raise IntergentError(f"unknown candidate: {candidate_ref}")
        cid = int(candidate["id"])
        unit = self.store.get_unit(int(candidate["unit_id"]))
        config = self.config
        branch = candidate["branch"]
        head = gitutil.rev_parse(self.root, branch)
        if head != candidate["head_commit"]:
            self.store.update_candidate(cid, head_commit=head)

        fp = compute_fingerprint(self.root, config, head)
        fp_id = self.store.get_or_create_fingerprint(
            cid, fp.fingerprint, fp.tree, fp.cmd_digest, fp.toolchain_digest, fp.policy_digest
        )
        self.store.conn.commit()
        if not force:
            cached = self.store.latest_verification_for_fingerprint(fp_id)
            if cached is not None and cached["status"] == "passed":
                return {
                    "candidate": cid,
                    "status": "passed",
                    "fingerprint": fp.fingerprint,
                    "from_cache": True,
                    "checks": [],
                }

        status, checks, duration = run_checks(self.root, config, head)
        self.store.add_verification(
            cid,
            fp_id,
            status,
            _checks_output(checks),
            duration,
        )
        if status == "passed":
            self.store.update_candidate(cid, status="verified")
            if unit is not None:
                self._release_unit(int(unit["id"]), status="released")
        else:
            self.store.update_candidate(cid, status="failed")
        self.store.conn.commit()
        self.store.event(
            "candidate.verified",
            unit_id=int(candidate["unit_id"]),
            candidate_id=cid,
            data={"status": status, "fingerprint": fp.fingerprint},
        )
        return {
            "candidate": cid,
            "status": status,
            "fingerprint": fp.fingerprint,
            "from_cache": False,
            "duration": duration,
            "checks": [c.to_dict() for c in checks],
        }

    def simulation(self, *, run_checks_flag: bool = True) -> dict[str, Any]:
        return planner.simulate(
            self.store, self.root, self.config, run_checks_flag=run_checks_flag
        )

    def review(self, candidate_ref: str | int) -> dict[str, Any]:
        candidate = self.store.get_candidate(candidate_ref)
        if candidate is None:
            raise IntergentError(f"unknown candidate: {candidate_ref}")
        unit = self.store.get_unit(int(candidate["unit_id"]))
        intent = self.store.get_intent(int(candidate["intent_id"])) if candidate.get("intent_id") else None
        scopes = (
            self.store.intent_scopes(int(candidate["intent_id"]))
            if candidate.get("intent_id")
            else []
        )
        verification = self.store.latest_verification(int(candidate["id"]))
        base = candidate.get("base_commit") or self.config.get("base") or "main"
        try:
            commits = gitutil.log_subjects(self.root, base, candidate["head_commit"])
            files = gitutil.diff_names(self.root, base, candidate["head_commit"])
        except IntergentError:
            commits, files = [], []
        wave = self._wave_for(candidate)
        findings = self._candidate_findings(candidate)
        risk = []
        if any(f.severity == "HIGH" for f in findings):
            risk.append("HIGH conflict")
        if any(s["kind"] in {"schema", "migration", "config"} for s in scopes):
            risk.append("schema/migration/config")
        if len(files) > 50:
            risk.append("large diff")
        return {
            "candidate": candidate,
            "unit": unit,
            "intent": intent,
            "scopes": scopes,
            "verification": verification,
            "commits": commits,
            "files": files,
            "wave": wave,
            "findings": [f.to_dict() for f in findings],
            "risk_flags": risk,
            "decisions": self.store.list_decisions(int(candidate["intent_id"])) if candidate.get("intent_id") else [],
        }

    def approve(self, candidate_ref: str | int, *, reason: str | None = None) -> dict[str, Any]:
        candidate = self.store.get_candidate(candidate_ref)
        if candidate is None:
            raise IntergentError(f"unknown candidate: {candidate_ref}")
        try:
            current_head = gitutil.rev_parse(self.root, candidate["branch"])
        except IntergentError:
            current_head = candidate["head_commit"]
        if current_head != candidate["head_commit"]:
            raise IntergentError(
                "candidate branch advanced since its last verification; run `intergent verify` again"
            )
        verification = self.store.latest_verification(int(candidate["id"]))
        if self.config.get("policy", {}).get("require_verification", True):
            if verification is None or verification["status"] != "passed":
                raise IntergentError(
                    "candidate has no passing local verification; run `intergent verify` first"
                )
        findings = self._candidate_findings(candidate)
        if findings:
            self.store.event(
                "candidate.approved_with_findings",
                candidate_id=int(candidate["id"]),
                data={"count": len(findings), "highest": findings[0].severity},
            )
        self.store.update_candidate(int(candidate["id"]), status="approved")
        intent_id = candidate.get("intent_id")
        if intent_id:
            self.store.add_decision(
                intent_id=int(intent_id),
                related_intent_id=None,
                verdict="human_approval",
                severity=None,
                rationale="candidate approved for landing",
                action="approve",
                reason=reason,
            )
        self.store.conn.commit()
        self.store.event("candidate.approved", candidate_id=int(candidate["id"]))
        return self.store.get_candidate(int(candidate["id"]))  # type: ignore[return-value]

    def reject(self, candidate_ref: str | int, *, reason: str | None = None) -> dict[str, Any]:
        candidate = self.store.get_candidate(candidate_ref)
        if candidate is None:
            raise IntergentError(f"unknown candidate: {candidate_ref}")
        self.store.update_candidate(int(candidate["id"]), status="rejected")
        self.store.conn.commit()
        self.store.event("candidate.rejected", candidate_id=int(candidate["id"]), data={"reason": reason})
        return self.store.get_candidate(int(candidate["id"]))  # type: ignore[return-value]

    def approve_all_verified(self, *, reason: str | None = None) -> list[dict[str, Any]]:
        approved = []
        for candidate in self.store.list_candidates(statuses=["verified"]):
            approved.append(self.approve(int(candidate["id"]), reason=reason))
        return approved

    def land(
        self,
        candidate_refs: list[str | int] | None = None,
        *,
        all_approved: bool = False,
        run_checks_flag: bool = True,
        cleanup: bool = False,
    ) -> list[dict[str, Any]]:
        if all_approved or not candidate_refs:
            ids = [int(c["id"]) for c in self.store.list_candidates(statuses=["approved"])]
        else:
            ids = []
            for ref in candidate_refs:
                candidate = self.store.get_candidate(ref)
                if candidate is None:
                    raise IntergentError(f"unknown candidate: {ref}")
                ids.append(int(candidate["id"]))
        if not ids:
            return []
        results = landing.land_candidates(
            self.store,
            self.root,
            self.config,
            ids,
            run_checks_flag=run_checks_flag,
            cleanup=cleanup,
        )
        return [r.to_dict() for r in results]

    def rebase(self, unit_ref: str | int, *, onto: str | None = None) -> dict[str, Any]:
        unit = self.store.require_unit(unit_ref)
        worktree = Path(unit["worktree"])
        if not gitutil.is_clean(worktree):
            raise IntergentError(
                "worktree has uncommitted changes; commit or discard before rebasing"
            )
        target = onto or self.config.get("base") or self.config.get("main_branch") or "main"
        result = gitutil.rebase_onto(worktree, target)
        if not result.ok:
            gitutil.git(worktree, "rebase", "--abort")
            raise IntergentError(f"rebase onto {target} failed: {result.stderr.strip()}")
        head = gitutil.head_commit(worktree)
        candidate = self.store.get_candidate(unit["name"])
        if candidate is not None:
            self.store.update_candidate(int(candidate["id"]), head_commit=head, status="ready")
        self.store.conn.commit()
        return {"unit": unit["name"], "onto": target, "head": head}

    def status(self) -> dict[str, Any]:
        self._reap_expired()
        units = self.list_units()
        candidates = self.store.list_candidates()
        waves = planner.plan_waves(
            self.store,
            self.root,
            self.config,
            [c for c in candidates if c["status"] in {"ready", "verified", "approved"}],
        )
        return {
            "root": str(self.root),
            "main_branch": self.config.get("main_branch"),
            "units": units,
            "candidates": candidates,
            "queue": self.store.queued_requests(),
            "waves": [w.to_dict() for w in waves],
            "decisions": self.store.list_decisions()[:20],
        }

    def gc(self) -> dict[str, Any]:
        removed = []
        for unit in self.store.list_units():
            if unit["state"] in {"landed", "released", "abandoned"}:
                path = Path(unit["worktree"])
                if path.exists():
                    gitutil.remove_worktree(self.root, path, force=True)
                    from .util import rmtree

                    rmtree(path)
                    removed.append(unit["name"])
        gitutil.prune_worktrees(self.root)
        scratch = self.root / ".intergent" / "scratch"
        if scratch.exists():
            from .util import rmtree

            rmtree(scratch)
        self.store.conn.commit()
        return {"removed_worktrees": removed}

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _candidate_findings(self, candidate: dict[str, Any]) -> list[Finding]:
        intent_id = candidate.get("intent_id")
        if not intent_id:
            return []
        intent = self.store.get_intent(int(intent_id))
        if intent is None:
            return []
        scope_rows = self.store.intent_scopes(int(intent_id))
        ref = self._intent_ref(
            {
                "id": int(intent_id),
                "unit_id": int(candidate["unit_id"]),
                "unit_name": candidate.get("unit_name", ""),
                "operation": intent["operation"],
                "scopes": scope_rows,
            }
        )
        others = self._other_refs(int(candidate["unit_id"]))
        others = [o for o in others if o.intent_id != int(intent_id)]
        return conflict.evaluate(ref, others)

    def _has_override(self, intent_id: Any) -> bool:
        if not intent_id:
            return False
        for decision in self.store.list_decisions(int(intent_id)):
            if decision.get("action") == "override":
                return True
        return False

    def _wave_for(self, candidate: dict[str, Any]) -> int | None:
        candidates = self.store.list_candidates(
            statuses=["ready", "verified", "approved", "blocked", "failed"]
        )
        waves = planner.plan_waves(self.store, self.root, self.config, candidates)
        for wave in waves:
            if any(int(c["id"]) == int(candidate["id"]) for c in wave.candidates):
                return wave.index
        return None

    def _blocker_from_findings(self, findings: list[Finding]) -> int | None:
        for finding in findings:
            if finding.rule == "FM-C001 destructive_vs_additive" and finding.asserted:
                row = self.store.get_intent(finding.other_intent_id)
                if row is not None:
                    return int(row["unit_id"])
        return None

    def _release_unit(self, unit_id: int, *, status: str) -> list[dict[str, Any]]:
        self.store.release_claims(unit_id, state=status)
        request = self.store.get_lock_request_for_unit(unit_id)
        if request is not None and request["status"] in {"granted", "queued", "needs_decision"}:
            self.store.set_lock_request_status(int(request["id"]), status)
        self.store.conn.commit()
        return self._promote_queue()

    def _promote_queue(self) -> list[dict[str, Any]]:
        promoted: list[dict[str, Any]] = []
        ttl = int(self.config.get("lease_ttl_seconds", 1800))
        queued = self.store.queued_requests()
        for request in queued:
            unit_id = int(request["unit_id"])
            requirements = {k: str(v) for k, v in _loads(request["requirements"]).items()}
            held = [
                HeldLock(
                    unit_id=int(c["unit_id"]),
                    unit_name=c["unit_name"],
                    node=c["node"],
                    mode=c["mode"],
                )
                for c in self.store.held_claims(exclude_unit=unit_id)
            ]
            blocker = locks.find_blocker(
                {node: Requirement(node=node, mode=mode, declared=True) for node, mode in requirements.items()},
                held,
            )
            if blocker is not None:
                # Keep the closest blocker recorded; do not grant out of order.
                continue
            self.store.set_lock_request_status(int(request["id"]), "granted")
            self.store.grant_claims(
                request_id=int(request["id"]),
                intent_id=int(request["intent_id"]),
                unit_id=unit_id,
                requirements=requirements,
                ttl_seconds=ttl,
            )
            self.store.set_intent_status(int(request["intent_id"]), "granted")
            unit = self.store.get_unit(unit_id)
            self.store.event(
                "lease.promoted",
                unit_id=unit_id,
                intent_id=int(request["intent_id"]),
                data={"unit": (unit or {}).get("name")},
            )
            promoted.append({"unit_name": (unit or {}).get("name", unit_id), "intent_id": request["intent_id"]})
        self.store.conn.commit()
        return promoted

    def _reap_expired(self) -> list[dict[str, Any]]:
        expired_units = self.store.expire_claims(now())
        reaped: list[dict[str, Any]] = []
        for unit_id in expired_units:
            self.store.release_claims(unit_id, state="expired")
            request = self.store.get_lock_request_for_unit(unit_id)
            if request is not None and request["status"] == "granted":
                self.store.set_lock_request_status(int(request["id"]), "expired", reason="lease TTL elapsed")
                self.store.set_intent_status(int(request["intent_id"]), "expired")
            unit = self.store.get_unit(unit_id)
            self.store.event("lease.expired", unit_id=unit_id, data={"unit": (unit or {}).get("name")})
            reaped.append({"unit_id": unit_id, "unit_name": (unit or {}).get("name")})
        if expired_units:
            self.store.conn.commit()
            self._promote_queue()
        return reaped

    def _log(self, kind: str, unit: dict[str, Any], intent_id: int, data: dict[str, Any]) -> None:
        self.store.event(kind, unit_id=int(unit["id"]), intent_id=intent_id, data=data)
        self.store.conn.commit()


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
def _loads(text: str) -> dict[str, Any]:
    import json

    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return {}


def _finding_intent(findings: list[Finding]) -> int | None:
    for finding in findings:
        if finding.rule == "FM-C001 destructive_vs_additive" and finding.asserted:
            return finding.other_intent_id
    return None


def _checks_output(checks: list[Any]) -> str:
    lines = []
    for check in checks:
        lines.append(f"[{check.status}] {check.name}: {check.command}")
        if check.output:
            lines.append(check.output[-2000:])
    return "\n".join(lines) or "(no checks configured)"


def _unique_branch(root: Path, session: str, name: str) -> str:
    base = f"ig/{slugify(session, 24)}/{slugify(name, 32)}"
    branch = base
    counter = 2
    while gitutil.branch_exists(root, branch):
        branch = f"{base}-{counter}"
        counter += 1
    return branch


def _unique_worktree(base_dir: Path, session: str, name: str) -> Path:
    candidate = base_dir / f"{slugify(session, 24)}-{slugify(name, 32)}"
    path = candidate
    counter = 2
    while path.exists():
        path = candidate.with_name(candidate.name + f"-{counter}")
        counter += 1
    return path


def _ensure_gitignore(root: Path) -> None:
    """Ignore ``.intergent/`` via the repo-local exclude file.

    Using ``.git/info/exclude`` (shared by all worktrees) keeps the main
    worktree clean, unlike creating an untracked ``.gitignore``.
    """
    entry = ".intergent/"
    res = gitutil.git(root, "rev-parse", "--git-common-dir", check=False)
    git_dir = Path(res.stdout.strip()) if res.ok else root / ".git"
    if not git_dir.is_absolute():
        git_dir = (root / git_dir).resolve()
    exclude = git_dir / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    content = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    if entry in {line.strip() for line in content.splitlines()}:
        return
    if content and not content.endswith("\n"):
        content += "\n"
    exclude.write_text(content + entry + "\n", encoding="utf-8")
