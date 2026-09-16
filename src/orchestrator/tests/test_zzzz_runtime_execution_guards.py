from __future__ import annotations

import json
import unittest
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
