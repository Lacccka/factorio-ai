from __future__ import annotations

import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from factorio_ai import architectural_execution_guard as architectural  # noqa: F401
from factorio_ai import app as base
from factorio_ai import route_executor


class RouteExecutorTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _plan() -> dict:
        return {
            "production_blocks": [],
            "material_routes": [
                {
                    "id": "coal-feed",
                    "belt": "transport-belt",
                    "source_mode": "new",
                    "source": {"x": 16.5, "y": -150.5},
                    "sink": {"x": 16.5, "y": -180.5},
                    "segments": [
                        {
                            "from": {"x": 16.5, "y": -150.5},
                            "to": {"x": 16.5, "y": -180.5},
                            "direction": "north",
                        }
                    ],
                }
            ],
            "placements": [],
            "removals": [],
            "power": {"pole_type": "medium-electric-pole", "pole_ids": []},
        }

    @staticmethod
    def _settings() -> SimpleNamespace:
        return SimpleNamespace(
            max_mutations=80,
            max_failed_mutations=8,
            tool_result_max_chars=20_000,
        )

    def test_segment_targets_expand_one_tile_grid(self):
        targets, error = route_executor._segment_targets(
            {
                "entity_name": "transport-belt",
                "from": {"x": 16.5, "y": -150.5},
                "to": {"x": 16.5, "y": -153.5},
                "direction": "north",
            }
        )
        self.assertIsNone(error)
        self.assertIsNotNone(targets)
        self.assertEqual(len(targets), 4)
        self.assertEqual(targets[-1]["y"], -153.5)

    async def test_executor_places_whole_segment_without_cloud_per_tile(self):
        metrics = base.RunMetrics(started_at=time.perf_counter())
        metrics.plan_validated = True
        metrics.validated_plan = self._plan()

        async def call_tool(session, name, arguments, max_chars):
            if name == "place_entity":
                return json.dumps(
                    {
                        "success": True,
                        "entity": arguments["entityName"],
                        "x": arguments["x"],
                        "y": arguments["y"],
                    }
                )
            raise AssertionError(name)

        with patch.object(base, "_call_tool", call_tool):
            result = await route_executor._build_route_segment(
                None,
                {
                    "entity_name": "transport-belt",
                    "from": {"x": 16.5, "y": -150.5},
                    "to": {"x": 16.5, "y": -153.5},
                    "direction": "north",
                },
                self._settings(),
                metrics,
                metrics.validated_plan,
            )

        self.assertTrue(result["success"], result)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["placed"], 4)
        self.assertEqual(metrics.mutation_calls, 4)

    async def test_executor_returns_compact_live_blocker_without_revoking_plan(self):
        metrics = base.RunMetrics(started_at=time.perf_counter())
        metrics.plan_validated = True
        metrics.validated_plan = self._plan()

        async def call_tool(session, name, arguments, max_chars):
            if name == "place_entity":
                if float(arguments["y"]) == -152.5:
                    return json.dumps(
                        {
                            "success": False,
                            "error": "invalid_position",
                            "entity": arguments["entityName"],
                            "x": arguments["x"],
                            "y": arguments["y"],
                        }
                    )
                return json.dumps({"success": True, "entity": arguments["entityName"]})
            if name == "inspect_entity":
                return json.dumps(
                    {
                        "success": True,
                        "entity": "steel-furnace",
                        "type": "furnace",
                        "position": {"x": arguments["x"], "y": arguments["y"]},
                        "direction": "north",
                    }
                )
            raise AssertionError(name)

        with patch.object(base, "_call_tool", call_tool):
            result = await route_executor._build_route_segment(
                None,
                {
                    "entity_name": "transport-belt",
                    "from": {"x": 16.5, "y": -150.5},
                    "to": {"x": 16.5, "y": -154.5},
                    "direction": "north",
                },
                self._settings(),
                metrics,
                metrics.validated_plan,
            )

        self.assertFalse(result["success"])
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["blocked_at"], {"x": 16.5, "y": -152.5})
        self.assertEqual(result["existing"]["entity"], "steel-furnace")
        self.assertTrue(metrics.plan_validated)
        self.assertEqual(metrics.failed_mutations, 0)

    async def test_local_detour_segment_is_authorized_inside_corridor(self):
        metrics = base.RunMetrics(started_at=time.perf_counter())
        metrics.plan_validated = True
        metrics.validated_plan = self._plan()

        async def call_tool(session, name, arguments, max_chars):
            if name == "place_entity":
                return json.dumps({"success": True, "entity": arguments["entityName"]})
            raise AssertionError(name)

        with patch.object(base, "_call_tool", call_tool):
            result = await route_executor._build_route_segment(
                None,
                {
                    "entity_name": "transport-belt",
                    "from": {"x": 19.5, "y": -166.5},
                    "to": {"x": 19.5, "y": -170.5},
                    "direction": "north",
                },
                self._settings(),
                metrics,
                metrics.validated_plan,
            )
        self.assertTrue(result["success"], result)


if __name__ == "__main__":
    unittest.main()
