"""Unit tests for the deterministic conflict engine."""

import unittest

from intergent.conflict import IntentRef, evaluate, requires_decision
from intergent.scopes import make_scope


def intent(intent_id, unit, op, specs):
    return IntentRef(
        intent_id=intent_id,
        unit_id=intent_id,
        unit_name=unit,
        operation=op,
        scopes=[(make_scope(*_split(spec)), op) for spec in specs],
    )


def _split(spec):
    kind, key = spec.split(":", 1)
    return kind, key


class ConflictTests(unittest.TestCase):
    def test_destructive_vs_additive_exact_is_high_and_needs_decision(self):
        a = intent(1, "alpha", "replace", ["symbol:src/a.py#Foo"])
        b = intent(2, "beta", "extend", ["symbol:src/a.py#Foo"])
        findings = evaluate(b, [a])
        self.assertTrue(findings)
        self.assertEqual(findings[0].rule, "FM-C001 destructive_vs_additive")
        self.assertEqual(findings[0].severity, "HIGH")
        self.assertTrue(findings[0].asserted)
        self.assertTrue(requires_decision(findings))

    def test_both_destructive_is_high(self):
        a = intent(1, "alpha", "replace", ["file:src/a.py"])
        b = intent(2, "beta", "remove", ["file:src/a.py"])
        findings = evaluate(b, [a])
        self.assertEqual(findings[0].rule, "FM-C002 divergent_rewrite")
        self.assertEqual(findings[0].severity, "HIGH")

    def test_both_additive_is_medium_not_blocking(self):
        a = intent(1, "alpha", "extend", ["config:app.timeout"])
        b = intent(2, "beta", "add", ["config:app.timeout"])
        findings = evaluate(b, [a])
        self.assertEqual(findings[0].rule, "FM-C003 shared_contract")
        self.assertEqual(findings[0].severity, "MEDIUM")
        self.assertFalse(requires_decision(findings))

    def test_disjoint_no_findings(self):
        a = intent(1, "alpha", "modify", ["file:src/a.py"])
        b = intent(2, "beta", "modify", ["file:src/b.py"])
        self.assertEqual(evaluate(b, [a]), [])

    def test_inferred_prefix_overlap_capped_below_high(self):
        a = intent(1, "alpha", "replace", ["symbol:src/a.py#Foo"])
        b = intent(2, "beta", "extend", ["file:src/a.py"])
        findings = evaluate(b, [a])
        self.assertTrue(findings)
        self.assertFalse(findings[0].asserted)
        self.assertTrue(all(f.severity != "HIGH" for f in findings))


if __name__ == "__main__":
    unittest.main()
