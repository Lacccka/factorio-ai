from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from factorio_ai.mutation_policy import classify_mutation_domains
from factorio_ai.world_model import SemanticWorldModel, semantic_snapshot


def _context_payload(context: str) -> dict:
    return json.loads(context.splitlines()[-1])


class SemanticWorldModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.tempdir.name)
        self.world = SemanticWorldModel(self.state_dir, "player-one")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_factory_survey_becomes_bounded_semantic_architecture(self):
        payload = {
            "status": "ok",
            "center_x": 10,
            "center_y": -5,
            "radius": 60,
            "belt_entity_count": 50,
            "reported_runs": 30,
            "belt_runs": [
                {
                    "axis": "horizontal",
                    "flow_direction": "east",
                    "fixed_coordinate": index + 0.5,
                    "start_coordinate": 0,
                    "end_coordinate": 40,
                    "tile_count": 41,
                    "belt": "transport-belt",
                    "sample_items": [{"name": "iron-plate", "count": 8}],
                }
                for index in range(30)
            ],
            "assembler_count": 12,
            "assembler_zones": [
                {
                    "assembler_count": 6,
                    "min_x": 1,
                    "max_x": 9,
                    "min_y": 2,
                    "max_y": 10,
                    "recipes": [{"recipe": "electronic-circuit", "count": 6}],
                }
            ],
        }
        snapshot = semantic_snapshot("survey_factory_layout", json.dumps(payload))
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        section, domains, facts = snapshot
        self.assertEqual(section, "architecture")
        self.assertIn("logistics", domains)
        self.assertEqual(len(facts["belt_runs"]), 16)
        self.assertNotIn("status", facts)

    def test_only_related_mutations_mark_fact_for_revalidation(self):
        survey = json.dumps(
            {
                "status": "ok",
                "center_x": 0,
                "center_y": 0,
                "radius": 60,
                "belt_runs": [
                    {
                        "axis": "horizontal",
                        "flow_direction": "east",
                        "fixed_coordinate": 4.5,
                        "start_coordinate": -20,
                        "end_coordinate": 30,
                        "tile_count": 51,
                    }
                ],
                "assembler_zones": [],
            }
        )
        self.assertTrue(self.world.observe("survey_factory_layout", {"radius": 60}, survey, 3))

        # Inventory-only crafting does not invalidate the physical bus architecture.
        self.world.record_mutation("craft", {"item": "iron-gear-wheel", "count": 10}, 4)
        context, count = self.world.context()
        self.assertEqual(count, 1)
        entry = _context_payload(context)["sections"]["architecture"][0]
        self.assertNotIn("may_be_affected_by", entry)

        # A later belt placement intersects architecture/logistics domains.
        self.world.record_mutation(
            "place_entity",
            {"entityName": "transport-belt", "x": 12.5, "y": 4.5},
            5,
        )
        context, count = self.world.context()
        self.assertEqual(count, 1)
        entry = _context_payload(context)["sections"]["architecture"][0]
        self.assertIn("may_be_affected_by", entry)
        rendered = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
        self.assertIn("transport-belt", rendered)
        self.assertIn('"x":12.5', rendered)

    def test_power_mutation_does_not_invalidate_resource_patch(self):
        resources = json.dumps(
            {
                "success": True,
                "resources": [
                    {"name": "iron-ore", "x": 120, "y": -40, "amount": 500000}
                ],
            }
        )
        self.assertTrue(self.world.observe("scan_resources", {"radius": 200}, resources, 7))
        self.world.record_mutation(
            "place_entity",
            {"entityName": "medium-electric-pole", "x": 10, "y": 10},
            8,
        )
        context, _ = self.world.context()
        entry = _context_payload(context)["sections"]["resources"][0]
        self.assertIn("iron-ore", json.dumps(entry))
        self.assertNotIn("may_be_affected_by", entry)

    def test_legacy_observations_can_seed_semantic_memory(self):
        knowledge = {
            "player_name": "player-one",
            "world_revision": 9,
            "observations": [
                {
                    "tool": "find_buildable_area",
                    "arguments": {"width": 20, "height": 20},
                    "output": json.dumps(
                        {
                            "success": True,
                            "x": 44,
                            "y": 12,
                            "width": 20,
                            "height": 20,
                        }
                    ),
                    "world_revision": 9,
                    "observed_at": "2026-09-16T10:00:00+00:00",
                }
            ],
            "recent_mutations": [],
        }
        self.assertEqual(self.world.import_legacy(knowledge), 1)
        context, count = self.world.context()
        self.assertEqual(count, 1)
        self.assertIn("build_areas", context)
        self.assertIn('"x":44', context)

    def test_runtime_mutation_policy_is_local(self):
        self.assertEqual(
            classify_mutation_domains("craft", {"item": "transport-belt"}),
            {"inventory"},
        )
        domains = classify_mutation_domains(
            "place_entity",
            {"entityName": "medium-electric-pole", "x": 1, "y": 2},
        )
        self.assertIn("power", domains)
        self.assertIn("geometry", domains)
        self.assertNotIn("resources", domains)


if __name__ == "__main__":
    unittest.main()
