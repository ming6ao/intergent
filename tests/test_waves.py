"""DAG wave planner (``intergent/waves.py``).

Pins the strict scheduling contract:
* waves are a deterministic projection of the DAG;
* per-wave size is capped by ``concurrency`` (default 3);
* ``depends_on`` forces a later wave;
* any declared-scope overlap forces a later wave.
"""

import unittest

from intergent.util import IntergentError
from intergent.waves import DEFAULT_WAVE_SIZE, plan_dag_waves


def node(nid, owns=None, depends_on=None):
    return {"id": nid, "owns": owns or [], "depends_on": depends_on or []}


class WavePlanTests(unittest.TestCase):
    def members(self, waves):
        return [w.members for w in waves]

    def test_independent_non_conflicting_nodes_share_a_wave(self):
        waves = plan_dag_waves(
            [
                node("w1", ["file:src/a.py"]),
                node("w2", ["file:src/b.py"]),
                node("w3", ["file:src/c.py"]),
            ]
        )
        self.assertEqual(self.members(waves), [["w1", "w2", "w3"]])

    def test_wave_size_caps_membership_and_opens_a_new_wave(self):
        nodes = [node(f"w{i}", [f"file:src/{i}.py"]) for i in range(5)]
        waves = plan_dag_waves(nodes, wave_size=3)
        self.assertEqual(self.members(waves), [["w0", "w1", "w2"], ["w3", "w4"]])

    def test_default_wave_size_is_three(self):
        nodes = [node(f"w{i}", [f"file:src/{i}.py"]) for i in range(4)]
        waves = plan_dag_waves(nodes)
        self.assertEqual(DEFAULT_WAVE_SIZE, 3)
        self.assertEqual([len(w.members) for w in waves], [3, 1])

    def test_dependency_forces_a_later_wave(self):
        waves = plan_dag_waves(
            [
                node("w2", ["file:src/b.py"], depends_on=["w1"]),
                node("w1", ["file:src/a.py"]),
            ]
        )
        self.assertEqual(self.members(waves), [["w1"], ["w2"]])

    def test_dependency_barrier_holds_even_when_wave_has_room(self):
        waves = plan_dag_waves(
            [
                node("w1", ["file:src/a.py"]),
                node("w2", ["file:src/b.py"], depends_on=["w1"]),
                node("w3", ["file:src/c.py"]),
            ]
        )
        # w3 may share w1's wave, but w2 cannot.
        self.assertEqual(self.members(waves), [["w1", "w3"], ["w2"]])

    def test_exact_file_conflict_moves_the_later_node(self):
        waves = plan_dag_waves(
            [
                node("w1", ["file:src/a.py"]),
                node("w2", ["file:src/a.py"]),
            ]
        )
        self.assertEqual(self.members(waves), [["w1"], ["w2"]])
        self.assertIn("w2", waves[1].conflicts)
        self.assertIn("w1", waves[1].conflicts["w2"])

    def test_directory_and_file_overlap_conflict(self):
        waves = plan_dag_waves(
            [
                node("w1", ["dir:src"]),
                node("w2", ["file:src/a.py"]),
            ]
        )
        self.assertEqual(self.members(waves), [["w1"], ["w2"]])

    def test_strict_policy_splits_even_additive_looking_scopes(self):
        # No operation is declared, so the strict policy still separates them.
        waves = plan_dag_waves(
            [
                node("w1", ["symbol:src/a.py#A.run"]),
                node("w2", ["symbol:src/a.py#A.run"]),
            ]
        )
        self.assertEqual(self.members(waves), [["w1"], ["w2"]])

    def test_nodes_without_owning_scopes_never_conflict(self):
        waves = plan_dag_waves([node("a"), node("b"), node("c")])
        self.assertEqual(self.members(waves), [["a", "b", "c"]])

    def test_dependency_is_min_wave_even_after_conflict(self):
        waves = plan_dag_waves(
            [
                node("w1", ["file:src/a.py"]),
                node("w2", ["file:src/a.py"]),
                node("w3", ["file:src/c.py"], depends_on=["w2"]),
            ]
        )
        # w2 conflicts with w1 -> wave 1; w3 must be strictly after w2.
        self.assertEqual(self.members(waves), [["w1"], ["w2"], ["w3"]])

    def test_planning_is_deterministic(self):
        nodes = [
            node("w2", ["file:src/b.py"], depends_on=["w1"]),
            node("w1", ["file:src/a.py"]),
            node("w3", ["file:src/a.py"]),
            node("w4", ["file:src/d.py"]),
        ]
        first = self.members(plan_dag_waves(nodes))
        second = self.members(plan_dag_waves(nodes))
        self.assertEqual(first, second)

    def test_unknown_dependency_raises(self):
        with self.assertRaises(IntergentError):
            plan_dag_waves([node("w1", depends_on=["ghost"])])

    def test_cycle_raises(self):
        with self.assertRaises(IntergentError):
            plan_dag_waves(
                [
                    node("w1", depends_on=["w2"]),
                    node("w2", depends_on=["w1"]),
                ]
            )

    def test_duplicate_id_raises(self):
        with self.assertRaises(IntergentError):
            plan_dag_waves([node("w1"), node("w1")])

    def test_wave_size_must_be_positive(self):
        with self.assertRaises(IntergentError):
            plan_dag_waves([node("w1")], wave_size=0)


if __name__ == "__main__":
    unittest.main()
