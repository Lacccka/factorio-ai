from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

# Keep this test file alphabetically last: importing optimized_app intentionally installs
# runtime monkey patches on the shared base/persistence modules.
from factorio_ai import app as base
from factorio_ai import optimized_app
from factorio_ai.persistence import PersistentRunState


class OptimizedRuntimeWiringTests(unittest.TestCase):
    def test_persistent_store_emits_semantic_context_and_precise_invalidation(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = PersistentRunState(Path(tempdir), "player-one", "Build circuits")
            store.record_observation(
                "survey_factory_layout",
                {"radius": 60},
                json.dumps(
                    {
                        "status": "ok",
                        "center_x": 0,
                        "center_y": 0,
                        "radius": 60,
                        "belt_runs": [
                            {
                                "axis": "horizontal",
                                "flow_direction": "east",
                                "fixed_coordinate": 3.5,
                                "start_coordinate": -10,
                                "end_coordinate": 40,
                                "tile_count": 51,
                            }
                        ],
                        "assembler_zones": [],
                    }
                ),
            )
            context, count = store.factory_context()
            self.assertEqual(count, 1)
            self.assertIn("SEMANTIC FACTORY WORLD MODEL", context)
            self.assertTrue((Path(tempdir) / "world_model.json").exists())

            # Runtime policy must not stale physical architecture merely because the
            # crafted inventory item happens to be a belt.
            store.mark_world_mutation("craft", {"item": "transport-belt", "count": 10})
            context, _ = store.factory_context()
            payload = json.loads(context.splitlines()[-1])
            entry = payload["sections"]["architecture"][0]
            self.assertNotIn("may_be_affected_by", entry)

    def test_budget_suffix_does_not_hide_original_failure(self):
        output = (
            '{"success":false,"error":"out_of_range","distance":12.1,"limit":10}'
            "\n\nMUTATION_BUDGET_REACHED: failed mutation limit reached (8/8)"
        )
        self.assertTrue(optimized_app._is_out_of_range_failure(output))
        self.assertTrue(optimized_app._is_tool_failure_budget_aware(output))


class PartialExecutionResumeTests(unittest.IsolatedAsyncioTestCase):
    async def test_matching_live_placement_is_accepted_on_resume(self):
        plan = {
            "production_blocks": [{"id": "ammo"}],
            "material_routes": [{"id": "iron"}],
            "placements": [
                {
                    "id": "asm-1",
                    "entity_name": "assembling-machine-2",
                    "x": -74.5,
                    "y": -310.5,
                    "direction": "north",
                }
            ],
            "power": {},
        }
        validations: list[dict] = []

        async def fake_validator(candidate, call_mcp, available):
            validations.append(candidate)
            placement = candidate["placements"][0]
            if placement.get("allow_replace_existing"):
                return {
                    "valid": True,
                    "status": "PLAN_VALID",
                    "issue_count": 0,
                    "warning_count": 0,
                    "issues": [],
                    "warnings": [],
                }
            return {
                "valid": False,
                "status": "PLAN_INVALID",
                "issue_count": 1,
                "warning_count": 0,
                "issues": [
                    {
                        "code": "cannot_place_entity",
                        "placement_id": "asm-1",
                    }
                ],
                "warnings": [],
            }

        async def call_mcp(name, arguments):
            self.assertEqual(name, "get_nearby_entities")
            return json.dumps(
                {
                    "entities": [
                        {
                            "name": "assembling-machine-2",
                            "x": -74.5,
                            "y": -310.5,
                            "direction": "north",
                        }
                    ]
                }
            )

        with patch("factorio_ai.optimized_app._PRIOR_VALIDATE_FACTORY_PLAN", fake_validator):
            result = await optimized_app._validate_factory_plan_idempotent(
                plan,
                call_mcp,
                {"get_nearby_entities"},
            )

        self.assertTrue(result["valid"])
        self.assertEqual(len(validations), 2)
        self.assertFalse(plan["placements"][0].get("allow_replace_existing", False))
        self.assertTrue(validations[1]["placements"][0]["allow_replace_existing"])
        self.assertEqual(result["warnings"][-1]["code"], "placements_already_satisfied")

    async def test_repeated_place_entity_becomes_successful_noop(self):
        calls: list[str] = []

        async def fake_call_tool(session, name, arguments, max_chars):
            calls.append(name)
            if name == "get_nearby_entities":
                return json.dumps(
                    {
                        "entities": [
                            {
                                "name": "inserter",
                                "x": -76.5,
                                "y": -310.5,
                                "direction": "west",
                            }
                        ]
                    }
                )
            raise AssertionError("place_entity must not be dispatched when exact entity already exists")

        with patch("factorio_ai.optimized_app._PRIOR_CALL_TOOL", fake_call_tool):
            output = await optimized_app._call_tool_idempotent(
                None,
                "place_entity",
                {
                    "entityName": "inserter",
                    "x": -76.5,
                    "y": -310.5,
                    "direction": "west",
                },
                20_000,
            )

        payload = json.loads(output)
        self.assertTrue(payload["success"])
        self.assertEqual(payload["status"], "already_present")
        self.assertEqual(calls, ["get_nearby_entities"])

    async def test_out_of_range_does_not_consume_failed_mutation_budget(self):
        metrics = base.RunMetrics(started_at=0.0)
        metrics.failed_mutations = 7
        settings = SimpleNamespace(max_mutations=80, max_failed_mutations=8)

        async def fake_persistent_execute(session, response, settings_arg, metrics_arg, tool_names, plan_gated):
            metrics_arg.failed_mutations += 1
            metrics_arg.mutation_budget_exhausted = True
            return [
                {
                    "type": "function_call_output",
                    "call_id": "p1",
                    "output": (
                        '{"success":false,"error":"out_of_range","distance":12.1,"limit":10}'
                        "\n\nMUTATION_BUDGET_REACHED: failed mutation limit reached (8/8)"
                    ),
                }
            ]

        with patch("factorio_ai.optimized_app._PRIOR_PERSISTENT_EXECUTE", fake_persistent_execute):
            outputs = await optimized_app._persistent_execute_range_tolerant(
                None,
                SimpleNamespace(output=[]),
                settings,
                metrics,
                set(),
                True,
            )

        self.assertEqual(metrics.failed_mutations, 7)
        self.assertFalse(metrics.mutation_budget_exhausted)
        self.assertNotIn("MUTATION_BUDGET_REACHED", outputs[0]["output"])


if __name__ == "__main__":
    unittest.main()
