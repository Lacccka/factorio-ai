from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from factorio_ai.persistence import PersistentRunState, goal_fingerprint


class PersistentRunStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.tempdir.name)
        self.store = PersistentRunState(self.state_dir, "player-one", "Build defense hub")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_factory_observation_is_reused_and_deduplicated(self):
        args = {"radius": 60, "centerX": 10, "centerY": -20}
        self.store.record_observation(
            "survey_factory_layout",
            args,
            json.dumps({"status": "ok", "belt_runs": [{"fixed_coordinate": 32.5}]}),
        )
        self.store.record_observation(
            "survey_factory_layout",
            args,
            json.dumps({"status": "ok", "belt_runs": [{"fixed_coordinate": 33.5}]}),
        )

        context, count = self.store.factory_context()
        self.assertEqual(count, 1)
        self.assertIn("33.5", context)
        self.assertNotIn("32.5", context)

    def test_failed_observation_is_not_persisted(self):
        self.store.record_observation(
            "survey_factory_layout",
            {"radius": 60},
            "MCP_TOOL_ERROR:\nboom",
        )
        context, count = self.store.factory_context()
        self.assertEqual(count, 0)
        self.assertEqual(context, "")

    def test_checkpoint_only_resumes_exact_normalized_goal(self):
        validation = {
            "status": "PLAN_INVALID",
            "issue_count": 2,
            "warning_count": 0,
            "issues": [{"code": "entity_without_power_coverage"}],
            "warnings": [],
        }
        plan = {"production_blocks": [{"id": "ammo"}], "placements": []}
        self.store.save_plan_checkpoint(4, plan, validation)

        same_goal = PersistentRunState(self.state_dir, "player-one", "  Build   defense hub  ")
        context, checkpoint = same_goal.plan_checkpoint_context()
        self.assertIsNotNone(checkpoint)
        self.assertIn("PLAN_INVALID", context)
        self.assertEqual(checkpoint["validation_attempt"], 4)
        self.assertEqual(goal_fingerprint("Build defense hub"), goal_fingerprint("  Build   defense hub  "))

        different_goal = PersistentRunState(self.state_dir, "player-one", "Build science hub")
        context, checkpoint = different_goal.plan_checkpoint_context()
        self.assertEqual(context, "")
        self.assertIsNone(checkpoint)

    def test_successful_mutation_marks_knowledge_stale_and_execution_progress(self):
        self.store.record_observation(
            "scan_resources",
            {"radius": 100},
            json.dumps({"resources": [{"name": "iron-ore"}]}),
        )
        self.store.save_plan_checkpoint(
            2,
            {"production_blocks": [{"id": "ammo"}], "placements": []},
            {
                "status": "PLAN_VALID",
                "issue_count": 0,
                "warning_count": 0,
                "issues": [],
                "warnings": [],
            },
        )
        self.store.mark_world_mutation("place_entity", {"entityName": "transport-belt"})
        self.store.record_plan_execution_step(
            "place_entity",
            {"entityName": "transport-belt"},
            '{"success":true}',
            False,
        )

        knowledge = json.loads((self.state_dir / "factory_knowledge.json").read_text(encoding="utf-8"))
        self.assertEqual(knowledge["last_world_mutation"]["tool"], "place_entity")

        _, checkpoint = self.store.plan_checkpoint_context()
        self.assertIsNotNone(checkpoint)
        self.assertTrue(checkpoint["execution"]["started"])
        self.assertEqual(checkpoint["execution"]["mutation_count"], 1)
        self.assertEqual(checkpoint["execution"]["last_step"]["tool"], "place_entity")


if __name__ == "__main__":
    unittest.main()
