from __future__ import annotations

import json
import unittest

from factorio_ai.planning import validate_factory_plan


class PlanningValidatorTests(unittest.IsolatedAsyncioTestCase):
    async def _call_mcp(self, name: str, args: dict) -> str:
        if name == "get_recipe_details":
            recipe = args["recipe"]
            if recipe == "stone-wall":
                return json.dumps(
                    {
                        "success": True,
                        "name": "stone-wall",
                        "energy": 0.5,
                        "ingredients": [{"type": "item", "name": "stone-brick", "amount": 5}],
                        "products": [{"type": "item", "name": "stone-wall", "amount": 1, "probability": 1}],
                    }
                )
            if recipe == "stone-brick":
                return json.dumps(
                    {
                        "success": True,
                        "name": "stone-brick",
                        "energy": 3.2,
                        "ingredients": [{"type": "item", "name": "stone", "amount": 2}],
                        "products": [{"type": "item", "name": "stone-brick", "amount": 1, "probability": 1}],
                    }
                )
            raise AssertionError(f"unexpected recipe {recipe}")
        if name == "get_entity_prototype":
            entity = args["entityName"]
            if entity == "assembling-machine-2":
                return json.dumps(
                    {
                        "entity": entity,
                        "tile_width": 3,
                        "tile_height": 3,
                        "type": "assembling-machine",
                        "crafting_speed": 0.75,
                        "has_burner": False,
                    }
                )
            if entity == "steel-furnace":
                return json.dumps(
                    {
                        "entity": entity,
                        "tile_width": 2,
                        "tile_height": 2,
                        "type": "furnace",
                        "crafting_speed": 2.0,
                        "energy_usage": 1500,
                        "has_burner": True,
                        "burner_effectivity": 1.0,
                        "burner_fuel_categories": ["chemical"],
                    }
                )
            if entity in {"small-electric-pole", "transport-belt"}:
                return json.dumps(
                    {
                        "entity": entity,
                        "tile_width": 1,
                        "tile_height": 1,
                        "type": "electric-pole" if entity == "small-electric-pole" else "transport-belt",
                        "has_burner": False,
                    }
                )
            raise AssertionError(f"unexpected prototype {entity}")
        if name == "get_item_fuel_info":
            item = args["itemName"]
            if item == "coal":
                return json.dumps(
                    {
                        "success": True,
                        "item": "coal",
                        "fuel_value": 4_000_000,
                        "fuel_category": "chemical",
                    }
                )
            return json.dumps({"success": False, "error": "unknown_item", "item": item})
        if name == "check_entity_placement_batch":
            placements = json.loads(args["placementsJson"])
            return json.dumps(
                {
                    "success": True,
                    "checked_count": len(placements),
                    "blocked_count": 0,
                    "results": [
                        {
                            "id": p["id"],
                            "entity_name": p["entity_name"],
                            "x": p["x"],
                            "y": p["y"],
                            "prototype_exists": True,
                            "can_place": True,
                        }
                        for p in placements
                    ],
                }
            )
        if name == "get_nearby_entities":
            x = float(args["centerX"])
            y = float(args["centerY"])
            if abs(x - 5.0) < 0.1 and abs(y) < 0.1:
                return json.dumps(
                    {
                        "entities": [
                            {
                                "name": "small-electric-pole",
                                "type": "electric-pole",
                                "x": 5.0,
                                "y": 0.0,
                                "direction": "north",
                            }
                        ]
                    }
                )
            return json.dumps(
                {
                    "entities": [
                        {
                            "name": "transport-belt",
                            "type": "transport-belt",
                            "x": x,
                            "y": y,
                            "direction": "north",
                        }
                    ]
                }
            )
        raise AssertionError(f"unexpected tool {name}")

    @staticmethod
    def _plan(machine_count: int, source_mode: str = "new", input_direction: str = "east") -> dict:
        input_end = {"x": 10.0, "y": 0.0} if input_direction == "east" else {"x": 0.0, "y": 10.0}
        return {
            "production_blocks": [
                {
                    "id": "walls",
                    "recipe": "stone-wall",
                    "machine": "assembling-machine-2",
                    "machine_count": machine_count,
                    "target_item": "stone-wall",
                }
            ],
            "material_routes": [
                {
                    "id": "brick-in",
                    "item": "stone-brick",
                    "role": "input",
                    "belt": "transport-belt",
                    "lane": "both",
                    "source_mode": source_mode,
                    "source": {"x": 0.0, "y": 0.0},
                    "sink": input_end,
                    "segments": [
                        {
                            "from": {"x": 0.0, "y": 0.0},
                            "to": input_end,
                            "direction": input_direction,
                        }
                    ],
                    "feeds_blocks": ["walls"],
                },
                {
                    "id": "wall-out",
                    "item": "stone-wall",
                    "role": "output",
                    "belt": "transport-belt",
                    "lane": "both",
                    "source_mode": "new",
                    "source": {"x": 0.0, "y": 4.0},
                    "sink": {"x": 10.0, "y": 4.0},
                    "segments": [
                        {
                            "from": {"x": 0.0, "y": 4.0},
                            "to": {"x": 10.0, "y": 4.0},
                            "direction": "east",
                        }
                    ],
                    "source_blocks": ["walls"],
                },
            ],
            "placements": [
                {
                    "id": "wall-asm",
                    "entity_name": "assembling-machine-2",
                    "x": 0.0,
                    "y": 2.0,
                    "direction": "north",
                    "block_id": "walls",
                },
                {
                    "id": "pole-1",
                    "entity_name": "small-electric-pole",
                    "x": 2.0,
                    "y": 2.0,
                    "direction": "north",
                },
            ],
            "power": {
                "pole_type": "small-electric-pole",
                "pole_ids": ["pole-1"],
                "existing_anchor": {"x": 5.0, "y": 0.0},
            },
        }

    @staticmethod
    def _burner_plan(include_fuel: bool = True) -> dict:
        routes = [
            {
                "id": "stone-in",
                "item": "stone",
                "role": "input",
                "belt": "transport-belt",
                "lane": "both",
                "source_mode": "new",
                "source": {"x": 0.0, "y": 0.0},
                "sink": {"x": 10.0, "y": 0.0},
                "segments": [
                    {
                        "from": {"x": 0.0, "y": 0.0},
                        "to": {"x": 10.0, "y": 0.0},
                        "direction": "east",
                    }
                ],
                "feeds_blocks": ["bricks"],
            },
            {
                "id": "brick-out",
                "item": "stone-brick",
                "role": "output",
                "belt": "transport-belt",
                "lane": "both",
                "source_mode": "new",
                "source": {"x": 0.0, "y": 4.0},
                "sink": {"x": 10.0, "y": 4.0},
                "segments": [
                    {
                        "from": {"x": 0.0, "y": 4.0},
                        "to": {"x": 10.0, "y": 4.0},
                        "direction": "east",
                    }
                ],
                "source_blocks": ["bricks"],
            },
        ]
        if include_fuel:
            routes.insert(
                1,
                {
                    "id": "coal-fuel",
                    "item": "coal",
                    "role": "fuel",
                    "belt": "transport-belt",
                    "lane": "left",
                    "source_mode": "new",
                    "source": {"x": 0.0, "y": 1.0},
                    "sink": {"x": 10.0, "y": 1.0},
                    "segments": [
                        {
                            "from": {"x": 0.0, "y": 1.0},
                            "to": {"x": 10.0, "y": 1.0},
                            "direction": "east",
                        }
                    ],
                    "feeds_blocks": ["bricks"],
                },
            )
        return {
            "production_blocks": [
                {
                    "id": "bricks",
                    "recipe": "stone-brick",
                    "machine": "steel-furnace",
                    "machine_count": 2,
                    "target_item": "stone-brick",
                }
            ],
            "material_routes": routes,
            "placements": [
                {
                    "id": "furnace-1",
                    "entity_name": "steel-furnace",
                    "x": 0.0,
                    "y": 2.0,
                    "direction": "north",
                    "block_id": "bricks",
                }
            ],
            "power": {
                "pole_type": "small-electric-pole",
                "pole_ids": [],
                "existing_anchor": {"x": 5.0, "y": 0.0},
            },
        }

    async def test_three_wall_assemblers_exceed_yellow_belt_capacity(self):
        result = await validate_factory_plan(
            self._plan(machine_count=3),
            self._call_mcp,
            {"get_recipe_details", "get_entity_prototype", "check_entity_placement_batch", "get_nearby_entities"},
        )
        self.assertFalse(result["valid"])
        self.assertIn("belt_capacity_exceeded", {issue["code"] for issue in result["issues"]})

    async def test_single_wall_assembler_plan_can_validate(self):
        result = await validate_factory_plan(
            self._plan(machine_count=1),
            self._call_mcp,
            {"get_recipe_details", "get_entity_prototype", "check_entity_placement_batch", "get_nearby_entities"},
        )
        self.assertTrue(result["valid"], result)
        self.assertEqual(result["production_blocks"][0]["actual_output_rate_per_second"], 1.5)
        self.assertEqual(result["material_routes"][0]["required_rate_per_second"], 7.5)

    async def test_extending_existing_belt_against_its_direction_is_rejected(self):
        plan = self._plan(machine_count=1, source_mode="extend", input_direction="south")
        result = await validate_factory_plan(
            plan,
            self._call_mcp,
            {"get_recipe_details", "get_entity_prototype", "check_entity_placement_batch", "get_nearby_entities"},
        )
        self.assertFalse(result["valid"])
        self.assertIn(
            "existing_belt_extension_direction_mismatch",
            {issue["code"] for issue in result["issues"]},
        )

    async def test_burner_furnace_coal_route_has_deterministic_fuel_rate(self):
        result = await validate_factory_plan(
            self._burner_plan(include_fuel=True),
            self._call_mcp,
            {
                "get_recipe_details",
                "get_entity_prototype",
                "get_item_fuel_info",
                "check_entity_placement_batch",
                "get_nearby_entities",
            },
        )
        self.assertTrue(result["valid"], result)
        fuel_route = next(route for route in result["material_routes"] if route["id"] == "coal-fuel")
        self.assertAlmostEqual(fuel_route["required_rate_per_second"], 0.045, places=6)

    async def test_burner_production_block_requires_fuel_route(self):
        result = await validate_factory_plan(
            self._burner_plan(include_fuel=False),
            self._call_mcp,
            {
                "get_recipe_details",
                "get_entity_prototype",
                "get_item_fuel_info",
                "check_entity_placement_batch",
                "get_nearby_entities",
            },
        )
        self.assertFalse(result["valid"])
        self.assertIn("burner_block_missing_fuel_route", {issue["code"] for issue in result["issues"]})


if __name__ == "__main__":
    unittest.main()
