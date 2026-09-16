from __future__ import annotations

import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from factorio_ai import adaptive_replan_guard as adaptive
from factorio_ai import app as base
from factorio_ai import route_preflight_guard as route_guard


class AdaptiveReplanTests(unittest.IsolatedAsyncioTestCase):
    async def test_route_preflight_rejects_live_furnace_inside_belt_segment(self):
        plan = {
            "material_routes": [
                {
                    "id": "coal-feed",
                    "belt": "transport-belt",
                    "source_mode": "new",
                    "source": {"x": 16.5, "y": -167.5},
                    "sink": {"x": 16.5, "y": -169.5},
                    "segments": [
                        {
                            "from": {"x": 16.5, "y": -167.5},
                            "to": {"x": 16.5, "y": -169.5},
                            "direction": "north",
                        }
                    ],
                }
            ],
            "placements": [],
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
            if name == "check_entity_placement_batch":
                placements = json.loads(arguments["placementsJson"])
                results = []
                for placement in placements:
                    blocked = abs(float(placement["y"]) + 168.5) < 0.01
                    results.append({**placement, "prototype_exists": True, "can_place": not blocked})
                return json.dumps({"success": True, "results": results})
            if name == "get_nearby_entities":
                return json.dumps(
                    {
                        "entities": [
                            {
                                "name": "steel-furnace",
                                "type": "furnace",
                                "x": 16.5,
                                "y": -168.5,
                                "direction": "north",
                            }
                        ]
                    }
                )
            raise AssertionError(name)

        with patch.object(route_guard, "_PRIOR_VALIDATE_FACTORY_PLAN", prior):
            result = await route_guard._validate_route_tile_preflight(
                plan,
                call_mcp,
                {"check_entity_placement_batch", "get_nearby_entities"},
            )

        self.assertFalse(result["valid"])
        issue = next(item for item in result["issues"] if item["code"] == "route_tile_collision")
        self.assertEqual(issue["route_id"], "coal-feed")
        self.assertEqual(issue["existing"]["name"], "steel-furnace")

    async def test_route_preflight_accepts_exact_existing_route_belt_on_resume(self):
        plan = {
            "material_routes": [
                {
                    "id": "coal-feed",
                    "belt": "transport-belt",
                    "source_mode": "new",
                    "source": {"x": 16.5, "y": -167.5},
                    "sink": {"x": 16.5, "y": -168.5},
                    "segments": [
                        {
                            "from": {"x": 16.5, "y": -167.5},
                            "to": {"x": 16.5, "y": -168.5},
                            "direction": "north",
                        }
                    ],
                }
            ],
            "placements": [],
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
            if name == "check_entity_placement_batch":
                placements = json.loads(arguments["placementsJson"])
                return json.dumps(
                    {
                        "success": True,
                        "results": [
                            {**placement, "prototype_exists": True, "can_place": False}
                            for placement in placements
                        ],
                    }
                )
            if name == "get_nearby_entities":
                return json.dumps(
                    {
                        "entities": [
                            {
                                "name": "transport-belt",
                                "type": "transport-belt",
                                "x": float(arguments["centerX"]),
                                "y": float(arguments["centerY"]),
                                "direction": "north",
                            }
                        ]
                    }
                )
            raise AssertionError(name)

        with patch.object(route_guard, "_PRIOR_VALIDATE_FACTORY_PLAN", prior):
            result = await route_guard._validate_route_tile_preflight(
                plan,
                call_mcp,
                {"check_entity_placement_batch", "get_nearby_entities"},
            )
        self.assertTrue(result["valid"], result)

    async def test_invalid_position_revokes_plan_and_enters_local_repair(self):
        metrics = base.RunMetrics(started_at=time.perf_counter())
        metrics.plan_validated = True
        metrics.plan_validation_attempts = 3
        metrics.validated_plan = {
            "production_blocks": [],
            "material_routes": [],
            "placements": [],
            "removals": [],
        }
        call = SimpleNamespace(
            type="function_call",
            name="place_entity",
            call_id="call-1",
            arguments=json.dumps(
                {
                    "entityName": "transport-belt",
                    "x": 16.5,
                    "y": -168.5,
                    "direction": "north",
                }
            ),
        )
        response = SimpleNamespace(output=[call])

        async def prior(session, response, settings, metrics, tool_names, plan_gated):
            item = response.output[0]
            return [
                {
                    "type": "function_call_output",
                    "call_id": item.call_id,
                    "output": json.dumps(
                        {
                            "success": False,
                            "error": "invalid_position",
                            "entity": "transport-belt",
                            "x": 16.5,
                            "y": -168.5,
                        }
                    ),
                }
            ]

        with patch.object(adaptive, "_PRIOR_EXECUTE_FUNCTION_CALLS", prior):
            outputs = await adaptive._execute_function_calls_adaptive(
                None,
                response,
                SimpleNamespace(),
                metrics,
                {"place_entity"},
                True,
            )

        self.assertFalse(metrics.plan_validated)
        validation = metrics._latest_plan_validation
        self.assertEqual(validation["status"], "PLAN_INVALID")
        self.assertEqual(validation["issues"][0]["code"], "execution_route_collision")
        self.assertIn("ADAPTIVE_REPLAN_REQUIRED", outputs[0]["output"])
        self.assertTrue(metrics._resume_repair_mode)


if __name__ == "__main__":
    unittest.main()
