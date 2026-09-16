from __future__ import annotations

import unittest

from factorio_ai import architectural_execution_guard as architectural


class ArchitecturalExecutionGuardTests(unittest.TestCase):
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
                            "direction": "south",
                        }
                    ],
                }
            ],
            "placements": [
                {
                    "id": "planned-pole",
                    "entity_name": "medium-electric-pole",
                    "x": -70.5,
                    "y": -307.5,
                    "direction": "north",
                },
                {
                    "id": "tap-iron",
                    "entity_name": "splitter",
                    "x": 32.0,
                    "y": -270.5,
                    "direction": "north",
                },
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
            "power": {
                "pole_type": "medium-electric-pole",
                "pole_ids": ["planned-pole"],
                "existing_anchor": {"x": -88.5, "y": -289.5},
            },
        }

    def test_local_belt_detour_is_allowed_inside_route_corridor(self):
        allowed, reason = architectural._architectural_mutation_authorized(
            "place_entity",
            {
                "entityName": "transport-belt",
                "x": 19.5,
                "y": -168.5,
                "direction": "south",
            },
            self._plan(),
        )
        self.assertTrue(allowed, reason)
        self.assertIn("local", reason)

    def test_local_underground_detour_is_allowed_inside_route_corridor(self):
        allowed, reason = architectural._architectural_mutation_authorized(
            "place_entity",
            {
                "entityName": "underground-belt",
                "x": 18.5,
                "y": -168.5,
                "direction": "south",
            },
            self._plan(),
        )
        self.assertTrue(allowed, reason)

    def test_belt_far_from_any_route_is_blocked(self):
        allowed, reason = architectural._architectural_mutation_authorized(
            "place_entity",
            {
                "entityName": "transport-belt",
                "x": 80.5,
                "y": -40.5,
                "direction": "east",
            },
            self._plan(),
        )
        self.assertFalse(allowed)
        self.assertIn("outside", reason)

    def test_planned_pole_can_shift_locally(self):
        allowed, reason = architectural._architectural_mutation_authorized(
            "place_entity",
            {
                "entityName": "medium-electric-pole",
                "x": -68.5,
                "y": -307.5,
                "direction": "north",
            },
            self._plan(),
        )
        self.assertTrue(allowed, reason)
        self.assertIn("local", reason)

    def test_off_plan_splitter_remains_blocked(self):
        allowed, _ = architectural._architectural_mutation_authorized(
            "place_entity",
            {
                "entityName": "splitter",
                "x": 19.0,
                "y": -168.5,
                "direction": "south",
            },
            self._plan(),
        )
        self.assertFalse(allowed)

    def test_destructive_removal_remains_exact(self):
        allowed, _ = architectural._architectural_mutation_authorized(
            "mine_entity",
            {"x": 32.5, "y": -270.5},
            self._plan(),
        )
        self.assertTrue(allowed)

        allowed, reason = architectural._architectural_mutation_authorized(
            "mine_entity",
            {"x": 16.5, "y": -168.5},
            self._plan(),
        )
        self.assertFalse(allowed)
        self.assertIn("removal", reason)


if __name__ == "__main__":
    unittest.main()
