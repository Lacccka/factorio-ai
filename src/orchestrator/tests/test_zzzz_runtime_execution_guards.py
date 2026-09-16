from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from factorio_ai import execution_batch_guard as batch_guard
from factorio_ai import power_reach_guard as power_guard


class RuntimeExecutionGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_mixed_small_anchor_to_medium_pole_uses_shorter_wire_reach(self):
        plan = {
            "placements": [
                {
                    "id": "m1",
                    "entity_name": "medium-electric-pole",
                    "x": 8.4,
                    "y": 0.0,
                    "direction": "north",
                }
            ],
            "power": {
                "pole_type": "medium-electric-pole",
                "pole_ids": ["m1"],
                "existing_anchor": {"x": 0.0, "y": 0.0},
            },
        }

        async def prior(plan, call_mcp, available_tool_names):
            return {
                "valid": True,
                "status": "PLAN_VALID",
                "issues": [],
                "warnings": [],
                "issue_count": 0,
                "warning_count": 0,
            }

        async def call_mcp(name, arguments):
            self.assertEqual(name, "get_nearby_entities")
            return json.dumps(
                {
                    "entities": [
                        {
                            "name": "small-electric-pole",
                            "type": "electric-pole",
                            "x": 0.0,
                            "y": 0.0,
                        }
                    ]
                }
            )

        with patch.object(power_guard, "_PRIOR_VALIDATE_FACTORY_PLAN", prior):
            result = await power_guard._validate_factory_plan_actual_wire_reach(
                plan,
                call_mcp,
                {"get_nearby_entities"},
            )

        self.assertFalse(result["valid"])
        self.assertIn(
            "power_network_disconnected_actual_wire_reach",
            {issue["code"] for issue in result["issues"]},
        )

    async def test_mixed_small_anchor_accepts_medium_pole_inside_small_reach(self):
        plan = {
            "placements": [
                {
                    "id": "m1",
                    "entity_name": "medium-electric-pole",
                    "x": 7.0,
                    "y": 0.0,
                    "direction": "north",
                }
            ],
            "power": {
                "pole_type": "medium-electric-pole",
                "pole_ids": ["m1"],
                "existing_anchor": {"x": 0.0, "y": 0.0},
            },
        }

        async def prior(plan, call_mcp, available_tool_names):
            return {
                "valid": True,
                "status": "PLAN_VALID",
                "issues": [],
                "warnings": [],
                "issue_count": 0,
                "warning_count": 0,
            }

        async def call_mcp(name, arguments):
            return json.dumps(
                {
                    "entities": [
                        {
                            "name": "small-electric-pole",
                            "type": "electric-pole",
                            "x": 0.0,
                            "y": 0.0,
                        }
                    ]
                }
            )

        with patch.object(power_guard, "_PRIOR_VALIDATE_FACTORY_PLAN", prior):
            result = await power_guard._validate_factory_plan_actual_wire_reach(
                plan,
                call_mcp,
                {"get_nearby_entities"},
            )
        self.assertTrue(result["valid"], result)

    def test_place_entity_multiple_requires_every_target_to_match_plan(self):
        plan = {
            "production_blocks": [],
            "material_routes": [
                {
                    "id": "r1",
                    "belt": "transport-belt",
                    "segments": [
                        {
                            "from": {"x": 1.5, "y": 2.5},
                            "to": {"x": 4.5, "y": 2.5},
                            "direction": "east",
                        }
                    ],
                }
            ],
            "placements": [
                {
                    "id": "p1",
                    "entity_name": "medium-electric-pole",
                    "x": 2.5,
                    "y": 5.5,
                    "direction": "north",
                }
            ],
            "removals": [],
        }
        allowed, _ = batch_guard._batch_authorized(
            plan,
            {
                "targets": json.dumps(
                    [
                        {"entityName": "transport-belt", "x": 2.5, "y": 2.5, "direction": "east"},
                        {"entityName": "medium-electric-pole", "x": 2.5, "y": 5.5, "direction": "north"},
                    ]
                )
            },
        )
        self.assertTrue(allowed)

        allowed, reason = batch_guard._batch_authorized(
            plan,
            {
                "targets": json.dumps(
                    [
                        {"entityName": "transport-belt", "x": 99.5, "y": 99.5, "direction": "east"},
                    ]
                )
            },
        )
        self.assertFalse(allowed)
        self.assertIn("not plan-authorized", reason)

    def test_execution_gets_separate_turn_budget_once(self):
        settings = SimpleNamespace(max_turns=40)
        metrics = SimpleNamespace(plan_validated=True)
        with patch.dict("os.environ", {"AGENT_EXECUTION_EXTRA_TURNS": "30"}):
            batch_guard._extend_execution_turn_budget(settings, metrics)
            batch_guard._extend_execution_turn_budget(settings, metrics)
        self.assertEqual(settings.max_turns, 70)
        self.assertTrue(metrics._execution_turn_budget_extended)


if __name__ == "__main__":
    unittest.main()
