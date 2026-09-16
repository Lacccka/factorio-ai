from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from factorio_ai import app as base
from factorio_ai import runtime_app  # noqa: F401 - installs the production runtime stack
from factorio_ai import architectural_execution_guard as architectural
from factorio_ai import execution_budget_guard as budget
from factorio_ai import owned_additive_guard as owned
from factorio_ai import persistent_app as persistent
from factorio_ai.persistence import PersistentRunState


class QualityRuntimeTests(unittest.TestCase):
    @staticmethod
    def _plan() -> dict:
        return {
            "production_blocks": [],
            "material_routes": [
                {
                    "id": "iron-feed",
                    "item": "iron-plate",
                    "role": "input",
                    "belt": "transport-belt",
                    "lane": "both",
                    "source_mode": "tap",
                    "source": {"x": 32.5, "y": -270.5},
                    "sink": {"x": -70.5, "y": -304.5},
                    "segments": [
                        {
                            "from": {"x": 32.5, "y": -270.5},
                            "to": {"x": -70.5, "y": -270.5},
                            "direction": "west",
                        },
                        {
                            "from": {"x": -70.5, "y": -270.5},
                            "to": {"x": -70.5, "y": -304.5},
                            "direction": "south",
                        },
                    ],
                }
            ],
            "placements": [],
            "removals": [],
            "power": {},
        }

    def test_route_geometry_can_deviate_materially_from_old_polyline(self):
        # This point is well away from the old horizontal segment but still inside the
        # source/sink work envelope. The exact-path guard used to reject it.
        allowed, reason = architectural._architectural_mutation_authorized(
            "place_entity",
            {
                "entityName": "transport-belt",
                "x": -20.5,
                "y": -282.5,
                "direction": "west",
            },
            self._plan(),
        )
        self.assertTrue(allowed, reason)
        self.assertIn("work area", reason)

    def test_additive_belt_outside_route_work_area_is_still_blocked(self):
        allowed, reason = architectural._architectural_mutation_authorized(
            "place_entity",
            {
                "entityName": "transport-belt",
                "x": 200.5,
                "y": 200.5,
                "direction": "east",
            },
            self._plan(),
        )
        self.assertFalse(allowed)
        self.assertIn("outside", reason)

    def test_checkpoint_tells_model_not_to_resubmit_after_runtime_revalidation(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = PersistentRunState(Path(tmp), "player", "goal")
            store.save_plan_checkpoint(
                2,
                self._plan(),
                {
                    "valid": True,
                    "status": "PLAN_VALID",
                    "issue_count": 0,
                    "warning_count": 0,
                    "issues": [],
                    "warnings": [],
                },
            )
            text, checkpoint = store.plan_checkpoint_context()
        self.assertIsNotNone(checkpoint)
        self.assertIn("do not submit the unchanged plan again", text)
        self.assertIn("waypoints, not immutable commands", text)

    def test_recent_ai_owned_belt_can_be_recognized_for_correction(self):
        metrics = base.RunMetrics(started_at=time.perf_counter())
        with tempfile.TemporaryDirectory() as tmp:
            store = PersistentRunState(Path(tmp), "player", "goal")
            store.save_plan_checkpoint(
                1,
                self._plan(),
                {
                    "valid": True,
                    "status": "PLAN_VALID",
                    "issue_count": 0,
                    "warning_count": 0,
                    "issues": [],
                    "warnings": [],
                },
            )
            store.record_plan_execution_step(
                "place_entity",
                {"entityName": "transport-belt", "x": 10.5, "y": -20.5, "direction": "west"},
                json.dumps({"success": True}),
                False,
            )
            persistent._RUN_STORES[id(metrics)] = store
            try:
                entity = owned._owned_additive_entity(metrics, {"x": 10.5, "y": -20.5})
            finally:
                persistent._RUN_STORES.pop(id(metrics), None)
        self.assertEqual(entity, "transport-belt")

    def test_execution_budget_is_policy_free_and_handles_frozen_settings(self):
        settings = base.Settings(
            provider="openai",
            player_name="player",
            rcon_host="127.0.0.1",
            rcon_port="27015",
            rcon_password="pw",
            mcp_project=Path("dummy"),
            model="gpt-5.6-sol",
            api_key="key",
            base_url="https://api.openai.com/v1",
            max_turns=40,
            max_mutations=80,
            max_failed_mutations=8,
            tool_result_max_chars=20_000,
        )
        metrics = base.RunMetrics(started_at=time.perf_counter(), plan_validated=True)
        with patch.dict("os.environ", {"AGENT_EXECUTION_EXTRA_TURNS": "20"}):
            budget._extend_execution_turn_budget(settings, metrics)
            budget._extend_execution_turn_budget(settings, metrics)
        self.assertEqual(settings.max_turns, 60)

    def test_runtime_prompt_explicitly_rejects_blind_coordinate_replay(self):
        self.assertIn("not for blindly replaying coordinates", base.SYSTEM_PROMPT)
        self.assertIn("A PLAN_VALID is NOT an immutable command", base.SYSTEM_PROMPT)
        self.assertNotIn("A PLAN_VALID is an executable contract, not permission to improvise", base.SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
