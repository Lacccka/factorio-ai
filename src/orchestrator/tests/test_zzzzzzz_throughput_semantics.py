from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from factorio_ai import throughput_semantics as throughput


class DesignThroughputSemanticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_wall_routes_use_declared_design_rate_not_machine_maximum(self):
        plan = {
            "production_blocks": [
                {
                    "id": "walls",
                    "recipe": "stone-wall",
                    "machine": "assembling-machine-2",
                    "machine_count": 1,
                    "target_item": "stone-wall",
                    "target_rate_per_second": 0.15,
                }
            ],
            "material_routes": [
                {
                    "id": "brick-in",
                    "item": "stone-brick",
                    "role": "input",
                    "belt": "transport-belt",
                    "lane": "both",
                    "feeds_blocks": ["walls"],
                },
                {
                    "id": "wall-out",
                    "item": "stone-wall",
                    "role": "output",
                    "belt": "transport-belt",
                    "lane": "both",
                    "source_blocks": ["walls"],
                },
            ],
        }

        async def prior(plan, call_mcp, available_tool_names):
            return {
                "valid": True,
                "status": "PLAN_VALID",
                "issues": [],
                "warnings": [],
                "production_blocks": [
                    {
                        "id": "walls",
                        "cycles_per_second": 1.5,
                        "target_item": "stone-wall",
                        "actual_output_rate_per_second": 1.5,
                    }
                ],
                "material_routes": [
                    {"id": "brick-in", "required_rate_per_second": 7.5, "capacity_per_second": 15.0},
                    {"id": "wall-out", "required_rate_per_second": 1.5, "capacity_per_second": 15.0},
                ],
            }

        async def call_mcp(name, args):
            if name == "get_recipe_details":
                return json.dumps(
                    {
                        "success": True,
                        "energy": 0.5,
                        "ingredients": [{"type": "item", "name": "stone-brick", "amount": 5}],
                        "products": [{"type": "item", "name": "stone-wall", "amount": 1, "probability": 1}],
                    }
                )
            if name == "get_entity_prototype":
                return json.dumps({"success": True, "entity": "assembling-machine-2", "has_burner": False})
            raise AssertionError(name)

        with patch.object(throughput, "_PRIOR_VALIDATE_FACTORY_PLAN", prior):
            result = await throughput._validate_factory_plan_design_throughput(
                plan,
                call_mcp,
                {"get_recipe_details", "get_entity_prototype"},
            )

        routes = {route["id"]: route for route in result["material_routes"]}
        self.assertAlmostEqual(routes["brick-in"]["required_rate_per_second"], 0.75, places=6)
        self.assertAlmostEqual(routes["wall-out"]["required_rate_per_second"], 0.15, places=6)
        block = result["production_blocks"][0]
        self.assertAlmostEqual(block["capacity_output_rate_per_second"], 1.5, places=6)
        self.assertAlmostEqual(block["planned_output_rate_per_second"], 0.15, places=6)
        self.assertAlmostEqual(block["design_utilization"], 0.1, places=6)

    async def test_large_furnace_block_no_longer_forces_full_load_and_warns_about_overbuild(self):
        plan = {
            "production_blocks": [
                {
                    "id": "bricks",
                    "recipe": "stone-brick",
                    "machine": "steel-furnace",
                    "machine_count": 12,
                    "target_item": "stone-brick",
                    "target_rate_per_second": 0.75,
                }
            ],
            "material_routes": [
                {
                    "id": "stone-in",
                    "item": "stone",
                    "role": "input",
                    "belt": "transport-belt",
                    "lane": "both",
                    "feeds_blocks": ["bricks"],
                },
                {
                    "id": "coal-in",
                    "item": "coal",
                    "role": "fuel",
                    "belt": "transport-belt",
                    "lane": "left",
                    "feeds_blocks": ["bricks"],
                },
                {
                    "id": "brick-out",
                    "item": "stone-brick",
                    "role": "output",
                    "belt": "transport-belt",
                    "lane": "both",
                    "source_blocks": ["bricks"],
                },
            ],
        }

        async def prior(plan, call_mcp, available_tool_names):
            return {
                "valid": True,
                "status": "PLAN_VALID",
                "issues": [],
                "warnings": [],
                "production_blocks": [
                    {
                        "id": "bricks",
                        "cycles_per_second": 7.5,
                        "target_item": "stone-brick",
                        "actual_output_rate_per_second": 7.5,
                    }
                ],
                "material_routes": [
                    {"id": "stone-in", "required_rate_per_second": 15.0, "capacity_per_second": 15.0},
                    {"id": "coal-in", "required_rate_per_second": 0.27, "capacity_per_second": 7.5},
                    {"id": "brick-out", "required_rate_per_second": 7.5, "capacity_per_second": 15.0},
                ],
            }

        async def call_mcp(name, args):
            if name == "get_recipe_details":
                return json.dumps(
                    {
                        "success": True,
                        "energy": 3.2,
                        "ingredients": [{"type": "item", "name": "stone", "amount": 2}],
                        "products": [{"type": "item", "name": "stone-brick", "amount": 1, "probability": 1}],
                    }
                )
            if name == "get_entity_prototype":
                return json.dumps(
                    {
                        "success": True,
                        "entity": "steel-furnace",
                        "has_burner": True,
                        "energy_usage": 1500,
                        "burner_effectivity": 1.0,
                    }
                )
            if name == "get_item_fuel_info":
                return json.dumps({"success": True, "item": "coal", "fuel_value": 4_000_000})
            raise AssertionError(name)

        with patch.object(throughput, "_PRIOR_VALIDATE_FACTORY_PLAN", prior):
            result = await throughput._validate_factory_plan_design_throughput(
                plan,
                call_mcp,
                {"get_recipe_details", "get_entity_prototype", "get_item_fuel_info"},
            )

        routes = {route["id"]: route for route in result["material_routes"]}
        self.assertAlmostEqual(routes["stone-in"]["required_rate_per_second"], 1.5, places=6)
        self.assertAlmostEqual(routes["brick-out"]["required_rate_per_second"], 0.75, places=6)
        self.assertAlmostEqual(routes["coal-in"]["required_rate_per_second"], 0.027, places=6)
        warning = next(w for w in result["warnings"] if w["code"] == "production_capacity_headroom")
        self.assertEqual(warning["minimum_machine_count"], 2)


if __name__ == "__main__":
    unittest.main()
