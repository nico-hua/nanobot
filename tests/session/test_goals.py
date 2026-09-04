"""Focused tests for JSON-safe session goal state."""

from __future__ import annotations

import json
import unittest

from nanobot.session import GoalState


class GoalStateTest(unittest.TestCase):
    def test_round_trips_through_json_safe_data(self) -> None:
        state = GoalState.create("Finish the migration.")

        serialized = state.to_dict()
        restored = GoalState.from_dict(json.loads(json.dumps(serialized)))

        self.assertEqual(restored, state)
        self.assertEqual(restored.status, "active")
        self.assertIsNone(restored.ended_at)
        self.assertNotIn("recap", serialized)

    def test_terminal_goal_records_end_time(self) -> None:
        state = GoalState.create("Resolve the issue.")

        failed = state.finish("failed")

        self.assertEqual(failed.status, "failed")
        self.assertIsNotNone(failed.ended_at)
        self.assertGreaterEqual(failed.updated_at, state.updated_at)
        self.assertNotIn("recap", failed.to_dict())
        self.assertEqual(GoalState.from_dict(failed.to_dict()), failed)
