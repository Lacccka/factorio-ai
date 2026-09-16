from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from factorio_ai import plan_execution_guard as guard


class PlanExecutionGuardTests(unittest.IsolatedAsyncioTestCase):
    def test_schema_requires_explicit_removals_and_tap_placement(self):
        plan_schema = guard.PLAN_TOOL_SCHEMA["parameters"]["properties"]["plan"]
        self.assertIn("removals", plan_schema["properties"])
        self.assertIn("removals", plan_schema["required"])
        route_properties = plan_schema["properties"]["material_routes"]["items"]["properties"]
        self.assertIn("tap_placement_id", route_properties)

    def test_vertical_splitter_uses_two_parallel_lane_centers(self):
        placement = {
            "entity_name": "splitter",
            "x": 32,
            "y": -270.5,
            "direction": "north",
        }
        lanes = guard._splitter_lane_centers(placement)
        self.assertEqual(lanes, ((31.5, -270.5), (32.5, -270.5)))
        source_and_adjacent = guard._lane_for_source(placement, 32.5, -270.5)
        self.assertEqual(source_and_adjacent, ((32.5, -270.5), (31.5, -270.5)))

    def test_execution_only_allows_exact_plan_mutations(self):
        plan = {
            "production_blocks": [],
            "material_routes": [
                {
                    "id": "r-iron",
                    "belt": "transport-belt",
                    "segments": [
                        {
                            "from": {"x": 32.5, "y": -270.5},
                            "to": {"x": 32.5, "y": -300.5},
                            "direction": "south",
                        }
                    ],
                }
            ],
            "placements": [
                {
                    "id": "tap-iron",
                    "entity_name": "splitter",
                    "x": 32,
                    "y": -270.5,
                    "direction": "north",
                }
            ],
            "removals": [
                {
                    "id": "remove-source",
                    "entity_name": "transport-belt",
                    "x": 32.5,
                    "y": -270.5,
                    "replaced_by": "tap-iron",
                }
            ],
        }

        allowed, _ = guard._mutation_authorized(
            "place_entity",
            {"entityName": "splitter", "x": 32, "y": -270.5, "direction": "north"},
            plan,
        )
        self.assertTrue(allowed)

        allowed, _ = guard._mutation_authorized(
            "place_entity",
            {"entityName": "splitter", "x": 33, "y": -270.5, "direction": "north"},
            plan,
        )
        self.assertFalse(allowed)

        allowed, _ = guard._mutation_authorized(
            "place_entity",
            {"entityName": "transport-belt", "x": 32.5, "y": -290.5, "direction": "south"},
            plan,
        )
        self.assertTrue(allowed)

        allowed, _ = guard._mutation_authorized(
            "mine_entity",
            {"x": 32.5, "y": -270.5},
            plan,
        )
        self.assertTrue(allowed)

        allowed, reason = guard._mutation_authorized(
            "mine_entity_multiple",
            {"targets": "[]"},
            plan,
        )
        self.assertFalse(allowed)
        self.assertIn("batch", reason)

        allowed, reason = guard._mutation_authorized(
            "remove_items",
            {"x": 22, "y": -177, "itemName": "iron-plate", "count": 100},
            plan,
        )
        self.assertFalse(allowed)
        self.assertIn("manual inventory logistics", reason)

    async def test_validator_rejects_splitter_that_consumes_adjacent_bus_lane(self):
        plan = {
            "material_routes": [
                {
                    "id": "r-iron",
                    "source_mode": "tap",
                    "source": {"x": 32.5, "y": -270.5},
                    "tap_placement_id": "tap-iron",
                }
            ],
            "placements": [
                {
                    "id": "tap-iron",
                    "entity_name": "splitter",
                    "x": 32,
                    "y": -270.5,
                    "direction": "north",
                    "allow_replace_existing": True,
                }
            ],
            "removals": [
                {
                    "id": "remove-iron-source",
                    "entity_name": "transport-belt",
                    "x": 32.5,
                    "y": -270.5,
                    "replaced_by": "tap-iron",
                }
            ],
        }

        async def prior_validator(plan, call_mcp, available_tool_names):
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
            x = float(arguments["centerX"])
            y = float(arguments["centerY"])
            entities = []
            if abs(x - 32.5) < 0.01 and abs(y + 270.5) < 0.01:
                entities.append({"name": "transport-belt", "x": 32.5, "y": -270.5, "direction": "north"})
            if abs(x - 31.5) < 0.01 and abs(y + 270.5) < 0.01:
                entities.append({"name": "transport-belt", "x": 31.5, "y": -270.5, "direction": "north"})
            # Placement-center queries use radius=2.0; return both belt lanes but no splitter.
            if abs(x - 32.0) < 0.01 and abs(y + 270.5) < 0.01:
                entities.extend(
                    [
                        {"name": "transport-belt", "x": 31.5, "y": -270.5, "direction": "north"},
                        {"name": "transport-belt", "x": 32.5, "y": -270.5, "direction": "north"},
                    ]
                )
            return json.dumps({"entities": entities})

        with patch.object(guard, "_PRIOR_VALIDATE_FACTORY_PLAN", prior_validator):
            validation = await guard._validate_factory_plan_conformant(
                plan,
                call_mcp,
                {"get_nearby_entities"},
            )

        codes = {issue["code"] for issue in validation["issues"]}
        self.assertIn("tap_would_overwrite_adjacent_belt", codes)
        self.assertEqual(validation["status"], "PLAN_INVALID")


if __name__ == "__main__":
    unittest.main()
