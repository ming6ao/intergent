"""Wave planning and combined-tree simulation (local integration).

Greedy wave packing over real mergeability: textual (``git merge-tree``) plus
declared-operation conflicts, with lease-ordering dependencies forcing a
candidate into a later wave.  Each wave's combined tree is materialized in a
scratch worktree so the configured checks run once over the combined result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import gitutil
from .conflict import Finding, IntentRef, evaluate
from .scopes import make_scope
from .store import Store
from .util import IntergentError, scratch_dir
from .verifier import CheckResult, run_checks

DESTRUCTIVE_RULES = {"FM-C001 destructive_vs_additive", "FM-C002 divergent_rewrite"}


@dataclass
class Wave:
    index: int
    candidates: list[dict[str, Any]] = field(default_factory=list)
    combined: str = ""
    findings: list[Finding] = field(default_factory=list)
    check_status: str | None = None
    checks: list[CheckResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "wave": self.index,
            "combined": self.combined,
            "members": [
                {"candidate": c["id"], "unit": c["unit_name"], "branch": c["branch"]}
                for c in self.candidates
            ],
            "findings": [f.to_dict() for f in self.findings],
            "check_status": self.check_status,
            "checks": [c.to_dict() for c in self.checks],
        }


def intent_ref(store: Store, candidate: dict[str, Any]) -> IntentRef:
    intent_id = candidate.get("intent_id")
    scopes: list[tuple[Any, str]] = []
    operation = "modify"
    if intent_id:
        intent = store.get_intent(int(intent_id))
        if intent:
            operation = intent["operation"]
            for row in store.intent_scopes(int(intent_id)):
                scopes.append((make_scope(row["kind"], row["key"]), row["operation"]))
    return IntentRef(
        intent_id=int(intent_id or 0),
        unit_id=int(candidate["unit_id"]),
        unit_name=candidate["unit_name"],
        operation=operation,
        scopes=scopes,
    )


def _synthetic_commit(root: Path, tree: str, parents: list[str], message: str) -> str:
    args = ["commit-tree", tree]
    for parent in parents:
        args += ["-p", parent]
    args += ["-m", message]
    return gitutil.git(root, *args, check=True).stdout.strip()


def _merge_into_wave(root: Path, combined: str, branch: str) -> tuple[bool, str, list[str]]:
    outcome = gitutil.merge_tree(root, combined, branch)
    if not outcome.clean or not outcome.tree:
        return False, combined, outcome.conflicts
    new_ref = _synthetic_commit(
        root, outcome.tree, [combined, gitutil.rev_parse(root, branch)], "ig wave combine"
    )
    return True, new_ref, []


def _waves_incompatible(a: IntentRef, b: IntentRef) -> bool:
    findings = evaluate(a, [b])
    return any(f.rule in DESTRUCTIVE_RULES for f in findings)


def _dependencies(store: Store, candidate: dict[str, Any]) -> set[int]:
    intent_id = candidate.get("intent_id")
    if not intent_id:
        return set()
    deps = store.dependencies_for_intent(int(intent_id))
    return {int(d["depends_on_intent_id"]) for d in deps}


def plan_waves(
    store: Store,
    root: Path,
    config: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> list[Wave]:
    if not candidates:
        return []
    base_ref = config.get("base") or config.get("main_branch") or "main"
    base_commit = gitutil.rev_parse(root, base_ref)
    ordered = sorted(
        candidates,
        key=lambda c: (-int(c.get("priority") or 0), float(c["created_at"]), int(c["id"])),
    )

    waves: list[Wave] = []
    intent_to_wave: dict[int, int] = {}
    refs: dict[int, IntentRef] = {}

    for candidate in ordered:
        ref = refs.setdefault(int(candidate["id"]), intent_ref(store, candidate))
        deps = _dependencies(store, candidate)
        min_wave = 0
        for dep in deps:
            if dep in intent_to_wave:
                min_wave = max(min_wave, intent_to_wave[dep] + 1)
        placed = False
        for wave in waves:
            if wave.index < min_wave:
                continue
            semantic_ok = all(
                not _waves_incompatible(ref, refs[int(member["id"])])
                for member in wave.candidates
            )
            if not semantic_ok:
                continue
            ok, new_ref, _conflicts = _merge_into_wave(root, wave.combined, candidate["branch"])
            if not ok:
                continue
            wave.candidates.append(candidate)
            wave.combined = new_ref
            placed = True
            break
        if not placed:
            wave = Wave(index=len(waves))
            ok, new_ref, _conflicts = _merge_into_wave(root, base_commit, candidate["branch"])
            if not ok:
                # A candidate that does not merge onto base is still placed in
                # its own wave, marked with the textual conflict for review.
                wave.combined = base_commit
                wave.candidates.append(candidate)
                wave.findings.extend(
                    Finding(
                        rule="FM-T001 textual_merge_conflict",
                        severity="HIGH",
                        asserted=True,
                        tier="exact",
                        other_unit="base",
                        other_intent_id=0,
                        a=candidate["branch"],
                        b=base_ref,
                        message="branch does not merge cleanly onto base",
                        suggestion="rebase onto the base branch and hand off again",
                    )
                )
            else:
                wave.combined = new_ref
                wave.candidates.append(candidate)
            waves.append(wave)
        if ref.intent_id:
            intent_to_wave[ref.intent_id] = wave.index
        if not ref.intent_id and candidate.get("intent_id"):
            intent_to_wave[int(candidate["intent_id"])] = wave.index

    # Attach semantic findings between members of the same wave (advisory).
    for wave in waves:
        members = [refs[int(c["id"])] for c in wave.candidates]
        for i, ref in enumerate(members):
            others = members[:i] + members[i + 1 :]
            for finding in evaluate(ref, others):
                if finding.rule not in DESTRUCTIVE_RULES:
                    wave.findings.append(finding)
    return waves


def simulate(
    store: Store,
    root: Path,
    config: dict[str, Any],
    *,
    statuses: list[str] | None = None,
    run_checks_flag: bool = True,
) -> dict[str, Any]:
    candidates = store.list_candidates(
        statuses=statuses or ["prepared", "pending"]
    )
    waves = plan_waves(store, root, config, candidates)
    scratch = scratch_dir(root)
    for wave in waves:
        if not run_checks_flag or not wave.combined:
            continue
        path = scratch / f"wave-{wave.index}-{abs(hash(wave.combined)) % 10_000_000}"
        try:
            gitutil.add_detached_worktree(root, path, wave.combined)
            status, results, _duration = run_checks(
                root, config, wave.combined, worktree=path
            )
            wave.check_status = status
            wave.checks = results
        except IntergentError:
            wave.check_status = "error"
        finally:
            gitutil.remove_worktree(root, path, force=True)
            from .util import rmtree

            rmtree(path)
            gitutil.prune_worktrees(root)
    return {
        "candidate_count": len(candidates),
        "waves": [w.to_dict() for w in waves],
        "overall": _overall_status(waves),
    }


def _overall_status(waves: list[Wave]) -> str:
    for wave in waves:
        if wave.findings and any(f.severity == "HIGH" for f in wave.findings):
            return "blocked"
    for wave in waves:
        if wave.check_status not in (None, "passed"):
            return "failed"
    return "pass"
