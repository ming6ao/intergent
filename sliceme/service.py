"""Service layer: the single owner of local-plane state.

Every adapter (CLI, pi extension) calls these functions.  This mirrors the
"one engine, many adapters / no adapter owns state" rule in
``docs/guide.md``.  Business rules live here; persistence lives in
``store``; git mutation lives in ``gitutil``.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from . import campaign, gitutil, integrate, sandbox
from .ownership import (
    DEFAULT_WAVE_SIZE,
    node_owns,
    parse_owns,
    path_within_owns,
    plan_dag_waves,
    validate_dag,
)
from .store import Store
from .util import (
    SlicemeError,
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
            raise SlicemeError("missing .sliceme/config.json")
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
        force: bool = False,
    ) -> dict[str, Any]:
        """Create the on-disk plane (config + state db) for a git repo."""
        from .util import ensure_parent, state_dir

        if not gitutil.is_git_repo(root):
            raise SlicemeError(f"{root} is not a git repository")
        state = state_dir(root)
        cfg_file = config_path(root)
        if cfg_file.exists() and not force:
            raise SlicemeError(
                "already initialised (.sliceme/config.json exists); use --force to reset config"
            )
        detected = gitutil.current_branch(root)
        main_branch = main_branch or detected
        if not main_branch:
            raise SlicemeError(
                "not on a branch; create or check out the campaign branch before start"
            )
        if not gitutil.branch_exists(root, main_branch):
            # `start` adopts the branch that is checked out and never creates
            # one.  A missing integration branch means the caller passed
            # `--main` explicitly (or HEAD is unborn): fail loudly instead of
            # silently branching off history the user did not choose.
            raise SlicemeError(
                f"branch '{main_branch}' does not exist; sliceme adopts the current "
                "branch and never creates one"
            )
        base = base or main_branch
        default_branch = integrate.found_default_branch(root, exclude=main_branch)
        config = {
            "version": 1,
            "main_branch": main_branch,
            "base": base,
            "default_branch": default_branch,
            "checks": checks or [],
            "policy": {"require_verification": True, "allow_auto_approve": []},
            "created_at": now(),
        }
        ensure_parent(cfg_file)
        write_json(cfg_file, config)
        state.mkdir(parents=True, exist_ok=True)
        store = Store(root)
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
            raise SlicemeError(
                f"branch '{main_branch}' does not exist; sliceme adopts the current "
                "branch and never creates one"
            )
        cfg = read_json(config_path(root))
        if cfg is None:
            raise SlicemeError("missing .sliceme/config.json")
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
        session: str | None = None,
        base: str | None = None,
        kind: str = "worker",
        main_branch: str | None = None,
        checks: list[dict[str, Any]] | None = None,
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
                raise SlicemeError(f"{start} is not a git repository")
            root = gitutil.toplevel(start)
            cls.init_plane(
                root,
                main_branch=main_branch,
                base=base,
                checks=checks,
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
        except SlicemeError:
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
        return self.store.get_session(session_id)  # type: ignore[return-value]

    def create_workspace(
        self,
        name: str,
        *,
        session: str | None = None,
        kind: str = "worker",
        base: str | None = None,
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
            )
        except Exception:
            gitutil.cleanup_worktree(self.root, worktree)
            raise
        self.store.conn.commit()
        return self.store.get_unit(unit_id)  # type: ignore[return-value]

    def list_units(self) -> list[dict[str, Any]]:
        return self.store.list_units()

    def unit_detail(self, unit_ref: str | int) -> dict[str, Any]:
        unit = self.store.require_unit(unit_ref)
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
        raise SlicemeError(
            "current directory is not an Sliceme unit worktree; "
            "run `sliceme workspace create` and cd into it, or pass --unit"
        )

    # ------------------------------------------------------------------
    # Work / candidates / verification
    # ------------------------------------------------------------------
    def commit(self, unit_ref: str | int, message: str) -> dict[str, Any]:
        unit = self.store.require_unit(unit_ref)
        worktree = Path(unit["worktree"])
        if not worktree.exists():
            raise SlicemeError(f"worktree missing: {worktree}")
        result = gitutil.commit_all(worktree, message)
        if not result.ok:
            raise SlicemeError(result.stderr.strip() or result.stdout.strip() or "git commit failed")
        return {"unit": unit["name"], "commit": gitutil.head_commit(worktree)}

    def finish(
        self, unit_ref: str | int, *, summary: str | None = None
    ) -> dict[str, Any]:
        unit = self.store.require_unit(unit_ref)
        worktree = Path(unit["worktree"])
        if not gitutil.is_clean(worktree):
            raise SlicemeError(
                "worktree has uncommitted changes; commit them (`sliceme commit`) before finishing"
            )
        head = gitutil.head_commit(worktree)
        violations = self._conformance_violations(unit, head)
        if violations:
            listing = ", ".join(sorted(violations)[:10])
            raise SlicemeError(
                f"commit touches paths outside unit '{unit['name']}' owned directories: "
                f"{listing}; the coordinator must widen `owns` or add a `depends_on` edge "
                "in dag.json (waves replan on the next status/spawn)"
            )
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
                branch=unit["branch"],
                head_commit=head,
                base_commit=unit.get("base_commit") or "",
                priority=0,
                summary=summary,
            )
        self.store.conn.commit()
        return self.store.get_candidate(cid)  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Wave scope: one worktree per wave, recorded by the single executor
    # ------------------------------------------------------------------
    def create_wave_workspace(
        self, wave_index: int, *, base: str | None = None, session: str | None = None
    ) -> dict[str, Any]:
        """Create (or reuse) the single branch + worktree for a wave.

        Same-wave nodes own disjoint directory subtrees, so one shared checkout
        is safe; the recorder maps each changed path back to its node.
        """
        name = f"wave-{int(wave_index)}"
        existing = self.store.get_unit(name)
        if existing is not None and Path(existing["worktree"]).exists():
            return existing
        config = self.config
        base_ref = base or config.get("base") or config.get("main_branch") or "main"
        base_commit = gitutil.rev_parse(self.root, base_ref)
        session_name = session or name
        sess = self.store.get_session(session_name)
        if sess is None:
            sess = self.create_session(session_name)
        session_id = int(sess["id"])
        branch = _unique_branch(self.root, session_name, name)
        worktree = _unique_worktree(worktrees_dir(self.root), session_name, name)
        gitutil.add_worktree(self.root, worktree, branch=branch, base=base_commit)
        try:
            unit_id = self.store.create_unit(
                session_id=session_id,
                name=name,
                kind="wave",
                worktree=str(worktree),
                branch=branch,
                base_commit=base_commit,
            )
        except Exception:
            gitutil.cleanup_worktree(self.root, worktree)
            raise
        self.store.conn.commit()
        return self.store.get_unit(unit_id)  # type: ignore[return-value]

    def wave_unit(self, wave_index: int) -> dict[str, Any] | None:
        return self.store.get_unit(f"wave-{int(wave_index)}")

    def record_wave(
        self,
        wave_index: int,
        *,
        message: str | None = None,
        summary: str | None = None,
    ) -> dict[str, Any]:
        """Record a shared wave worktree: conformance, then per-node commits.

        Every changed path is attributed to exactly one same-wave node by its
        owned directories; each node gets one commit and a prepared candidate
        on the shared wave branch.
        """
        branch = self.config.get("main_branch")
        dag = campaign.load_dag(self.root, branch) if branch else None
        if not dag or not dag.get("nodes"):
            raise SlicemeError("record_wave needs a campaign DAG")
        validate_dag(list(dag["nodes"]))
        wave_size = int(dag.get("concurrency") or DEFAULT_WAVE_SIZE)
        waves = plan_dag_waves(list(dag["nodes"]), wave_size=wave_size)
        wave = next((w for w in waves if w.index == int(wave_index)), None)
        if wave is None:
            raise SlicemeError(f"unknown wave: {wave_index}")
        unit = self.wave_unit(wave_index)
        if unit is None:
            raise SlicemeError(
                f"wave {wave_index} has no workspace; run `exec --open --wave {wave_index}` first"
            )
        by_id = {str(node["id"]): node for node in dag["nodes"]}
        members = [by_id[node_id] for node_id in wave.members if node_id in by_id]
        return self._record_wave_commits(
            unit, int(wave_index), members, message=message, summary=summary
        )

    def _record_wave_commits(
        self,
        unit: dict[str, Any],
        wave_index: int,
        members: list[dict[str, Any]],
        *,
        message: str | None,
        summary: str | None,
    ) -> dict[str, Any]:
        worktree = Path(unit["worktree"])
        if not worktree.exists():
            raise SlicemeError(f"wave worktree missing: {worktree}")
        base = unit.get("base_commit") or gitutil.rev_parse(self.root, unit["branch"])
        gitutil.git(worktree, "add", "-A", check=False)
        entries = _changed_entries(worktree, base)
        owners = {str(node["id"]): node_owns(node) for node in members}
        assignment: dict[str, list[str]] = {node_id: [] for node_id in owners}
        violations: list[str] = []
        for status, path, old in entries:
            new_owners = _owners_of(path, owners)
            old_owners = _owners_of(old, owners) if old else new_owners
            if (
                len(new_owners) != 1
                or len(old_owners) != 1
                or new_owners[0] != old_owners[0]
            ):
                violations.append(_describe_violation(status, path, old, new_owners, old_owners))
                continue
            node_id = new_owners[0]
            assignment[node_id].append(path)
            if old:
                assignment[node_id].append(old)
        if violations:
            raise SlicemeError(
                "wave conformance failed; every changed path must map to exactly one "
                "wave node's owned directories: " + "; ".join(violations[:10])
            )
        created: list[dict[str, Any]] = []
        for node in members:
            node_id = str(node["id"])
            paths = sorted(set(assignment.get(node_id) or []))
            if not paths:
                continue
            note = f"{node_id}: {message}" if message else f"{node_id}: wave {wave_index}"
            result = gitutil.git(worktree, "commit", "-m", note, "--", *paths, check=False)
            if not result.ok:
                raise SlicemeError(
                    result.stderr.strip()
                    or result.stdout.strip()
                    or f"commit failed for {node_id}"
                )
            head = gitutil.head_commit(worktree)
            cid = self.store.create_candidate(
                unit_id=int(unit["id"]),
                branch=unit["branch"],
                head_commit=head,
                base_commit=base,
                priority=0,
                summary=summary,
                node=node_id,
            )
            created.append(self.store.get_candidate(cid))
        self.store.conn.commit()
        return {
            "wave": int(wave_index),
            "unit": unit["name"],
            "branch": unit["branch"],
            "worktree": str(worktree),
            "candidates": created,
            "changed": [path for _, path, _ in entries],
        }

    # ------------------------------------------------------------------
    # Plan conformance
    # ------------------------------------------------------------------
    def owned_dirs(self, unit_name: str) -> list[str] | None:
        """The DAG node's owned directories, or ``None`` on a non-campaign plane.

        Re-spawned units are named ``<node>-a<attempt>``; the suffix is stripped
        so conformance still resolves the original node.
        """
        branch = self.config.get("main_branch")
        if not branch:
            return None
        dag = campaign.load_dag(self.root, branch)
        if not dag or not dag.get("nodes"):
            return None
        names = {unit_name}
        stripped = re.sub(r"-a\d+$", "", unit_name)
        names.add(stripped)
        for node in dag["nodes"]:
            if str(node.get("id")) in names:
                return parse_owns(node.get("owns") or [])
        return None

    def _conformance_violations(self, unit: dict[str, Any], head: str) -> list[str]:
        """Changed paths outside the node's owned directories (empty when fine)."""
        owns = self.owned_dirs(str(unit["name"]))
        base = unit.get("base_commit") or ""
        if owns is None or not base:
            return []
        changed = gitutil.changed_files(self.root, base, head)
        return [path for path in changed if not path_within_owns(path, owns)]

    def simulation(self, *, run_checks_flag: bool = True) -> dict[str, Any]:
        return integrate.simulate(
            self.store, self.root, self.config, run_checks_flag=run_checks_flag
        )

    def executor(self):
        """Build the single sandboxed verification executor for this plane."""
        from .executor import Executor

        branch = self.config.get("main_branch")
        dag = campaign.load_dag(self.root, branch) if branch else None
        return Executor(self.root, self.store, self.config, dag=dag)

    def sandbox_info(self, *, gpu_required: bool = False) -> dict[str, Any]:
        """Resolve and validate the project sandbox gate (never raises)."""
        branch = self.config.get("main_branch")
        dag = campaign.load_dag(self.root, branch) if branch else None
        required = sandbox.is_required(dag, self.config)
        try:
            profile = sandbox.require_sandbox(
                dag, self.config, root=self.root, gpu_required=gpu_required
            )
        except SlicemeError as exc:
            return {"ok": False, "required": required, "error": str(exc)}
        return {
            "ok": True,
            "required": required,
            "manifest": profile.manifest,
            "digest": profile.digest(),
            "sandbox": profile.to_dict(),
        }

    def status(self) -> dict[str, Any]:
        branch = self.config.get("main_branch")
        units = [self._project_unit(u, branch) for u in self.list_units()]
        candidates = self.store.list_candidates()
        waves = integrate.plan_waves(
            self.store,
            self.root,
            self.config,
            [c for c in candidates if c["status"] in {"prepared", "pending"}],
        )
        dag_waves, dag_waves_error = self._dag_waves(branch)
        return {
            "root": str(self.root),
            "main_branch": branch,
            "feature_branch": branch,
            "default_branch": self.config.get("default_branch") or integrate.found_default_branch(self.root),
            "units": units,
            "candidates": candidates,
            "waves": [w.to_dict() for w in waves],
            "dag_waves": dag_waves,
            "dag_waves_error": dag_waves_error,
            "executor": self.store.job_counts(),
            "sandbox": self.sandbox_info(),
        }

    def _dag_waves(self, branch: str | None) -> tuple[list[dict[str, Any]], str | None]:
        """Compute the scheduler's wave projection of the campaign DAG.

        The DAG is the only authored schedule; waves are derived here from
        ``owns``/``depends_on`` and the campaign's ``concurrency`` cap.  A
        malformed or cyclic DAG is reported rather than crashing ``status``.
        """
        if not branch:
            return [], None
        dag = campaign.load_dag(self.root, branch)
        if not dag or not dag.get("nodes"):
            return [], None
        wave_size = int(dag.get("concurrency") or DEFAULT_WAVE_SIZE)
        try:
            validate_dag(list(dag["nodes"]))
            planned = plan_dag_waves(list(dag["nodes"]), wave_size=wave_size)
        except SlicemeError as exc:
            return [], str(exc)
        return [w.to_dict() for w in planned], None

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
            raise SlicemeError("cleanup must be one of: none, worktrees, all")
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
        return campaign.write_report(
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
        for log in sorted(self.root.glob(f".sliceme/{campaign.branch_key(branch)}.worker_*.log")):
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
            # `sliceme/<unit>` branch is disposable; otherwise one branch leaks per
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

        scratch = self.root / ".sliceme" / "scratch"
        rmtree(scratch)
        self.store.conn.commit()
        return {"removed_worktrees": removed, "pruned_branches": pruned_branches}


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
def _owners_of(path: str, owners: dict[str, list[str]]) -> list[str]:
    return [node_id for node_id, owns in owners.items() if path_within_owns(path, owns)]


def _describe_violation(
    status: str,
    path: str,
    old: str | None,
    new_owners: list[str],
    old_owners: list[str],
) -> str:
    if old:
        return f"{status} {old} -> {path} spans nodes {old_owners}/{new_owners}"
    if not new_owners:
        return f"{status} {path} is outside every wave node"
    return f"{status} {path} is claimed by {new_owners}"


def _changed_entries(worktree: Path, base: str) -> list[tuple[str, str, str | None]]:
    """Staged changes as ``(status, path, old_path)``, rename-aware."""
    result = gitutil.git(
        worktree, "diff", "--cached", "--name-status", "-M", base, check=False
    )
    if not result.ok:
        return []
    entries: list[tuple[str, str, str | None]] = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0]
        if status[:1] in {"R", "C"} and len(parts) >= 3:
            entries.append((status, parts[2], parts[1]))
        else:
            entries.append((status, parts[1], None))
    return entries


def _session_unit_slugs(session: str, name: str) -> tuple[str, str]:
    """Slug the session and unit, collapsing the default ``session == name``.

    ``create_workspace`` defaults the session to the unit name, so without this
    a plain ``start`` produced ``<name>-<name>`` worktree directories and
    ``sliceme/<name>/<name>`` branches.
    """
    session_slug = slugify(session, 24)
    name_slug = slugify(name, 32)
    if session_slug == name_slug:
        return session_slug, session_slug
    return session_slug, name_slug


def _unique_branch(root: Path, session: str, name: str) -> str:
    session_slug, name_slug = _session_unit_slugs(session, name)
    base = f"sliceme/{session_slug}/{name_slug}" if session_slug != name_slug else f"sliceme/{name_slug}"
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
    """Ignore ``.sliceme/`` via the repo-local exclude file.

    Using ``.git/info/exclude`` (shared by all worktrees) keeps the main
    worktree clean, unlike creating an untracked ``.gitignore``.
    """
    entry = ".sliceme/"
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
