"""Deterministic conflict engine (``docs/conflict-engine.md``).

All verdicts here are computed from declared scopes and operations.  No model is
in the verdict path; LLM output may only ever be tagged ``inferred`` and capped
below HIGH elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .scopes import (
    ADDITIVE,
    DESTRUCTIVE,
    Scope,
    is_destructive,
    jaccard,
    scope_chain,
    tokens,
)

HIGH = "HIGH"
MEDIUM = "MEDIUM"
LOW = "LOW"

SEVERITY_ORDER = {LOW: 0, MEDIUM: 1, HIGH: 2}


@dataclass
class IntentRef:
    intent_id: int
    unit_id: int
    unit_name: str
    operation: str
    scopes: list[tuple[Scope, str]] = field(default_factory=list)


@dataclass
class Finding:
    rule: str
    severity: str
    asserted: bool
    tier: str
    other_unit: str
    other_intent_id: int
    a: str
    b: str
    message: str
    suggestion: str = ""

    def to_dict(self) -> dict:
        return {
            "rule": self.rule,
            "severity": self.severity,
            "asserted": self.asserted,
            "tier": self.tier,
            "other_unit": self.other_unit,
            "other_intent_id": self.other_intent_id,
            "a": self.a,
            "b": self.b,
            "message": self.message,
            "suggestion": self.suggestion,
        }


@dataclass
class Match:
    scope_a: Scope
    scope_b: Scope
    tier: str
    score: float
    asserted: bool


def match_scopes(a: Scope, b: Scope) -> Match | None:
    if a.node == b.node:
        return Match(a, b, "exact", 1.0, True)
    if a.canonical == b.canonical and a.kind != b.kind:
        return Match(a, b, "same_key", 0.90, False)
    chain_a = set(scope_chain(a.kind, a.canonical))
    chain_b = set(scope_chain(b.kind, b.canonical))
    if a.node in chain_b or b.node in chain_a:
        return Match(a, b, "related", 0.85, False)
    score = jaccard(tokens(a.kind, a.canonical), tokens(b.kind, b.canonical))
    if score >= 0.66:
        return Match(a, b, "token", round(0.78 + (score - 0.66) * 0.2, 3), False)
    return None


def _suggestion(kind_a: str, kind_b: str) -> str:
    kinds = {kind_a, kind_b}
    if kinds & {"schema", "migration", "config"}:
        return "agree an explicit migration/config ordering before editing"
    if kinds & {"api"}:
        return "version the API or extract a stable interface"
    return "extract a stable abstraction, or sequence the two changes"


def evaluate(
    candidate: IntentRef, others: list[IntentRef]
) -> list[Finding]:
    """Compare one intent against other active intents."""
    findings: list[Finding] = []
    for other in others:
        for scope_a, op_a in candidate.scopes:
            for scope_b, op_b in other.scopes:
                match = match_scopes(scope_a, scope_b)
                if match is None:
                    continue
                finding = _rule_for(candidate, other, scope_a, op_a, scope_b, op_b, match)
                if finding is not None:
                    findings.append(finding)
    findings.sort(
        key=lambda f: (SEVERITY_ORDER[f.severity], f.asserted, f.rule), reverse=True
    )
    return findings


def _rule_for(
    candidate: IntentRef,
    other: IntentRef,
    scope_a: Scope,
    op_a: str,
    scope_b: Scope,
    op_b: str,
    match: Match,
) -> Finding | None:
    dest_a = is_destructive(op_a)
    dest_b = is_destructive(op_b)
    asserted = match.asserted
    base = dict(
        asserted=asserted,
        tier=match.tier,
        other_unit=other.unit_name,
        other_intent_id=other.intent_id,
        a=f"{op_a} {scope_a.kind}:{scope_a.canonical}",
        b=f"{op_b} {scope_b.kind}:{scope_b.canonical}",
        suggestion=_suggestion(scope_a.kind, scope_b.kind),
    )
    if dest_a and not dest_b or (dest_b and not dest_a):
        return Finding(
            rule="FM-C001 destructive_vs_additive",
            severity=HIGH if asserted else MEDIUM,
            message=(
                f"{candidate.unit_name} and {other.unit_name} overlap on "
                f"{scope_a.kind}:{scope_a.canonical} with destructive and additive "
                "operations; waiting would extend a replaced contract"
            ),
            **base,
        )
    if dest_a and dest_b:
        return Finding(
            rule="FM-C002 divergent_rewrite",
            severity=HIGH if asserted else MEDIUM,
            message=(
                f"{candidate.unit_name} and {other.unit_name} both rewrite "
                f"{scope_a.kind}:{scope_a.canonical}"
            ),
            **base,
        )
    if op_a in ADDITIVE and op_b in ADDITIVE:
        return Finding(
            rule="FM-C003 shared_contract",
            severity=MEDIUM if asserted else LOW,
            message=(
                f"{candidate.unit_name} and {other.unit_name} both extend "
                f"{scope_a.kind}:{scope_a.canonical}; co-test the combined result"
            ),
            **base,
        )
    return None


def requires_decision(findings: list[Finding]) -> bool:
    """Only asserted destructive-vs-additive needs an explicit human decision."""
    return any(
        f.rule == "FM-C001 destructive_vs_additive" and f.asserted and f.severity == HIGH
        for f in findings
    )


def has_blocking(findings: list[Finding]) -> bool:
    return any(f.severity == HIGH for f in findings)


def summarize(findings: list[Finding]) -> str:
    if not findings:
        return "no conflicts"
    top = findings[0]
    count = len(findings)
    return f"{count} finding(s), highest {top.severity} ({top.rule})"
