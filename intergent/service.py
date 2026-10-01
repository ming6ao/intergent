"""Service layer: the single owner of local-plane state.

Every adapter (CLI, pi extension) calls these functions.  This mirrors the
"one engine, many adapters / no adapter owns state" rule in
``docs/architecture.md``.  Business rules live here; persistence lives in
``store``; git mutation lives in ``gitutil``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from . import campaign, conflict, gitutil, integrate, locks, planner, report as report_mod
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
        detected = gitutil.current_branch(root)
        main_branch = main_branch or detected
        if not main_branch:
            raise IntergentError(
                "not on a branch; create or check out the campaign branch before start"
            )
        if not gitutil.branch_exists(root, main_branch):
            # `start` adopts the branch that is checked out and never creates
            # one.  A missing integration branch means the caller passed
            # `--main` explicitly (or HEAD is unborn): fail loudly instead of
            # silently branching off history the user did not choose.
            raise IntergentError(
                f"branch '{main_branch}' does not exist; intergent adopts the current "
                "branch and never creates one"
            )
        base = base or main_branch
        default_branch = integrate.found_default_branch(root, exclude=main_branch)
        config = {
            "version": 1,
            "main_branch": main_branch,
            "base": base,
            "default_branch": default_branch,
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
    def _retarget_plane(
        cls, root: Path, *, main_branch: str, base: str | None = None
    ) -> None:
        """Point an existing plane's integration branch at **an existing** branch.

        Used by ``start --main`` so a campaign adopts the checked-out branch even
        when the plane already exists.  It never creates a branch: a missing one
        is an error.
        """
        if not gitutil.branch_exists(root, main_branch):
            raise IntergentError(
                f"branch '{main_branch}' does not exist; intergent adopts the current "
                "branch and never creates one"
            )
        cfg = read_json(config_path(root))
        if cfg is None:
            raise IntergentError("missing .intergent/config.json")
        if cfg.get("main_branch") == main_branch:
            return
        cfg["main_branch"] = main_branch
        cfg["base"] = base or main_branch
        write_json(config_path(root), cfg)

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
        no_unit: bool = False,
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
        elif main_branch:
            # An existing plane keeps its identity; re-point its integration
            # branch only when the caller names one explicitly.  `campaign
            # start` uses this to adopt the checked-out branch, and — like
            # `init_plane` — it never creates a branch.
            cls._retarget_plane(root, main_branch=main_branch, base=base)

        # Keep the exclude entry fresh even when the plane already existed and
        # the repo's .git/info/exclude was reset (e.g. re-cloned metadata).
        _ensure_gitignore(root)

        if no_unit:
            return {
                "root": str(root),
                "initialized": initialized,
                "created": False,
                "unit": None,
                "branch": None,
                "worktree": None,
            }

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
            gitutil.cleanup_worktree(self.root, worktree)
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
        return self._project_unit(unit, self.config.get("main_branch"))

    def current_unit(self, path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
        """Resolve the unit whose worktree contains *path* (default cwd).

        Lets an agent launched inside its own worktree call the CLI without
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
        if unit["state"] != "working":
            raise IntergentError(f"unit {unit['name']} is {unit['state']}, not working")
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
                reason="destructive vs additive overlap; the coordinator must re-plan",
            )
            self.store.set_intent_status(intent_id, "needs_decision")
            self.store.conn.commit()
            self._log("declare", unit, intent_id, {"status": "needs_decision"})
            return {
                "intent_id": intent_id,
                "status": "needs_decision",
                "requirements": req_map,
                "findings": [f.to_dict() for f in findings],
                "request_id": request_id,
                "blocker": blocker,
                "options": ["replan"],
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
        # Retire the unit so `status --gc` can prune its worktree.  (The
        # internal `_release_unit` is also used to supersede an intent on
        # re-declare, where the unit must stay active.)
        self.store.set_unit_state(int(unit["id"]), "closed")
        self.store.conn.commit()
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
            (c for c in existing if int(c["unit_id"]) == int(unit["id"]) and c["status"] in {"prepared", "failed", "blocked"}),
            None,
        )
        if candidate is not None:
            self.store.update_candidate(int(candidate["id"]), head_commit=head, status="prepared")
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
        self.store.conn.commit()
        self.store.event("candidate.prepared", unit_id=int(unit["id"]), candidate_id=cid)
        return self.store.get_candidate(cid)  # type: ignore[return-value]

    def simulation(self, *, run_checks_flag: bool = True) -> dict[str, Any]:
        return planner.simulate(
            self.store, self.root, self.config, run_checks_flag=run_checks_flag
        )

    def status(self) -> dict[str, Any]:
        self._reap_expired()
        branch = self.config.get("main_branch")
        units = [self._project_unit(u, branch) for u in self.list_units()]
        candidates = self.store.list_candidates()
        waves = planner.plan_waves(
            self.store,
            self.root,
            self.config,
            [c for c in candidates if c["status"] in {"prepared", "pending"}],
        )
        return {
            "root": str(self.root),
            "main_branch": branch,
            "feature_branch": branch,
            "default_branch": self.config.get("default_branch") or integrate.found_default_branch(self.root),
            "units": units,
            "candidates": candidates,
            "queue": self.store.queued_requests(),
            "waves": [w.to_dict() for w in waves],
        }

    def _project_unit(self, unit: dict[str, Any], branch: str | None) -> dict[str, Any]:
        """Add the campaign columns the dashboard needs (§6.3).

        ``node``/``log`` come from the campaign layout; ``candidate`` and
        ``verification`` are the unit's latest candidate row and its latest
        recorded verdict.
        """
        projected = dict(unit)
        unit_id = int(unit["id"])
        candidates = [
            c for c in self.store.list_candidates() if int(c["unit_id"]) == unit_id
        ]
        latest = candidates[-1] if candidates else None
        verification = (
            self.store.latest_verification(int(latest["id"])) if latest is not None else None
        )
        projected["node"] = unit["name"]
        projected["log"] = str(
            campaign.worker_log_path(self.root, branch or "main", unit["name"])
        )
        projected["candidate"] = int(latest["id"]) if latest is not None else None
        projected["verification"] = verification
        return projected

    def integrate(
        self,
        *,
        node: str | None = None,
        acceptance: list[str] | None = None,
        gpu: str = "none",
        check_only: bool = False,
        cleanup: str = "none",
        run_checks_flag: bool = True,
    ) -> dict[str, Any]:
        """Merge prepared candidates onto the feature branch (agent-callable)."""
        if cleanup not in {"none", "worktrees", "all"}:
            raise IntergentError("cleanup must be one of: none, worktrees, all")
        results = integrate.integrate(
            self.store,
            self.root,
            self.config,
            node=node,
            acceptance=acceptance,
            gpu=gpu,
            check_only=check_only,
            run_checks_flag=run_checks_flag,
        )
        self._promote_queue()
        cleanup_result: dict[str, Any] | None = None
        artifacts_removed: list[str] = []
        if cleanup in {"worktrees", "all"}:
            cleanup_result = self.gc()
        if cleanup == "all":
            artifacts_removed = self.remove_campaign_artifacts(keep_report=True)
        return {
            "main_branch": self.config.get("main_branch"),
            "node": node,
            "check_only": check_only,
            "results": [r.to_dict() for r in results],
            "cleanup": cleanup_result,
            "artifacts_removed": artifacts_removed,
        }

    def report(
        self, *, narrative: str | None = None, design: str | None = None
    ) -> dict[str, Any]:
        """Write the deterministic campaign report plus an optional narrative."""
        return report_mod.write_report(
            self.root, self.config, self.store, narrative=narrative, design=design
        )

    def remove_campaign_artifacts(self, *, keep_report: bool = True) -> list[str]:
        """Delete ``<branch-key>`` dag/state/worker logs (report kept by default)."""
        branch = self.config.get("main_branch") or "main"
        removed: list[str] = []
        paths = [
            campaign.dag_path(self.root, branch),
            campaign.state_path(self.root, branch),
        ]
        removed.extend(self._remove_files(paths))
        for log in sorted(self.root.glob(f".intergent/{campaign.branch_key(branch)}.worker_*.log")):
            removed.extend(self._remove_files([log]))
        if not keep_report:
            removed.extend(self._remove_files([campaign.report_path(self.root, branch)]))
        return removed

    @staticmethod
    def _remove_files(paths: list[Path]) -> list[str]:
        removed: list[str] = []
        for path in paths:
            try:
                if path.is_file() or path.is_symlink():
                    path.unlink()
                    removed.append(str(path))
            except OSError:
                continue
        return removed

    def gc(self) -> dict[str, Any]:
        removed = []
        pruned_branches = []
        for unit in self.store.list_units():
            if unit["state"] not in {"landed", "closed"}:
                continue
            path = Path(unit["worktree"])
            branch = unit.get("branch")
            registered = bool(branch) and gitutil.worktree_for_branch(self.root, branch) is not None
            if path.exists() or registered:
                removed.append(unit["name"])
            # Landed content is already on main as a squashed commit, so its
            # `ig/<unit>` branch is disposable; otherwise one branch leaks per
            # landed unit.  Closed units may hold unmerged work, so keep their
            # branches.  ``cleanup_worktree`` prunes stale metadata before
            # deleting the branch, so a hand-deleted worktree no longer blocks it.
            drop_branch = unit["state"] == "landed" and branch
            branch_existed = bool(drop_branch) and gitutil.branch_exists(self.root, branch)
            gitutil.cleanup_worktree(
                self.root, path, branch=branch if drop_branch else None
            )
            if branch_existed:
                pruned_branches.append(branch)
        gitutil.prune_worktrees(self.root)
        from .util import rmtree

        scratch = self.root / ".intergent" / "scratch"
        rmtree(scratch)
        self.store.conn.commit()
        return {"removed_worktrees": removed, "pruned_branches": pruned_branches}

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
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


def _session_unit_slugs(session: str, name: str) -> tuple[str, str]:
    """Slug the session and unit, collapsing the default ``session == name``.

    ``create_workspace`` defaults the session to the unit name, so without this
    a plain ``start`` produced ``<name>-<name>`` worktree directories and
    ``ig/<name>/<name>`` branches.
    """
    session_slug = slugify(session, 24)
    name_slug = slugify(name, 32)
    if session_slug == name_slug:
        return session_slug, session_slug
    return session_slug, name_slug


def _unique_branch(root: Path, session: str, name: str) -> str:
    session_slug, name_slug = _session_unit_slugs(session, name)
    base = f"ig/{session_slug}/{name_slug}" if session_slug != name_slug else f"ig/{name_slug}"
    branch = base
    counter = 2
    while gitutil.branch_exists(root, branch):
        branch = f"{base}-{counter}"
        counter += 1
    return branch


def _unique_worktree(base_dir: Path, session: str, name: str) -> Path:
    session_slug, name_slug = _session_unit_slugs(session, name)
    combined = f"{session_slug}-{name_slug}" if session_slug != name_slug else name_slug
    candidate = base_dir / combined
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
