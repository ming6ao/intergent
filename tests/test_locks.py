"""Unit tests for the multi-granularity lock manager."""

import unittest

from intergent.locks import (
    HeldLock,
    compatible,
    find_blocker,
    requirement_closure,
)
from intergent.scopes import parse_scope_specs


def reqs(specs, op="modify"):
    return requirement_closure(parse_scope_specs(specs, op))


class CompatibilityTests(unittest.TestCase):
    def test_matrix(self):
        self.assertTrue(compatible("IS", "IS"))
        self.assertTrue(compatible("IX", "IX"))
        self.assertTrue(compatible("S", "S"))
        self.assertFalse(compatible("X", "S"))
        self.assertFalse(compatible("IX", "S"))
        self.assertFalse(compatible("X", "IS"))

    def test_symmetric(self):
        for a in ("IS", "IX", "S", "SIX", "X"):
            for b in ("IS", "IX", "S", "SIX", "X"):
                self.assertEqual(compatible(a, b), compatible(b, a))


class ClosureTests(unittest.TestCase):
    def test_additive_uses_shared(self):
        closure = reqs(["file:src/a.py"])
        self.assertEqual(closure["file:src/a.py"].mode, "S")
        self.assertEqual(closure["dir:src/"].mode, "IS")
        self.assertTrue(closure["file:src/a.py"].declared)

    def test_destructive_uses_exclusive(self):
        closure = reqs(["file:src/a.py"], "replace")
        self.assertEqual(closure["file:src/a.py"].mode, "X")
        self.assertEqual(closure["dir:src/"].mode, "IX")

    def test_two_symbols_same_file_only_share_intention(self):
        closure = reqs(["symbol:src/a.py#A", "symbol:src/a.py#B"], "replace")
        self.assertEqual(closure["file:src/a.py"].mode, "IX")

    def test_find_blocker_exact(self):
        held = [HeldLock(2, "beta", "config:app.timeout", "S")]
        self.assertIsNotNone(find_blocker(reqs(["config:app.timeout"], "replace"), held))

    def test_find_blocker_hierarchical(self):
        held = [HeldLock(2, "beta", "file:src/a.py", "X")]
        self.assertIsNotNone(find_blocker(reqs(["symbol:src/a.py#A"]), held))

    def test_no_blocker_disjoint(self):
        held = [HeldLock(2, "beta", "file:src/b.py", "S")]
        self.assertIsNone(find_blocker(reqs(["symbol:src/a.py#A"]), held))


if __name__ == "__main__":
    unittest.main()
