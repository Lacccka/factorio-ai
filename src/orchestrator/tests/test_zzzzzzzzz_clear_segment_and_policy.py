from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from factorio_ai import app as base
from factorio_ai import runtime_app  # noqa: F401 - install production runtime stack
from factorio_ai import clear_belt_segment as clear
from factorio_ai import persistence
from factorio_ai import plan_policy_revision as policy


class ClearBeltSegmentTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _settings() -> base.Settings:
        return base.Settings(
            provider="openai",
            player_name="player",
            rcon_host="127.0.0.1",
            rcon_port="27015",
            rcon_password="pw",
            mcp_project=Path("dummy"),
            model="gpt-5.6-sol",
            api_key="key",
            base_url="https://api.openai.com/v1",
            max_turns=40,
            max_mutations=80,
            max_failed_mutations=8,
            tool_result_max_chars=20_000,
        )

    @staticmethod
    def _plan() -> dict:
        return {
            "production_blocks": [],
            "material_routes": [
                {
                    "id": "feed",
                    "item": "iron-plate",
                    "role": "input",
                    "belt": "transport-belt",
                    "lane": "both",
                    "source_mode": "extend",
                    "source": {"x": 0.5, "y": -10.5},
                    "sink": {"x": 20.5, "y": -10.5},
                    "segments": [
                        {
                            "from": {"x": 0.5, "y": -10.5},
                            "to": {"x": 20.5, "y": -10.5},
                            "direction": "east",
                        }
                    ],
                }
            ],
            "placements": [],
            "removals": [],
            "power": {},
        }

    async def test_clear_segment_preflights_then_builds_whole_run_in_one_model_action(self):
        metrics = base.RunMetrics(
            started_at=time.perf_counter(),
            plan_validated=True,
            validated_plan=self._plan(),
        )
        seen: list[tuple[str, dict]] = []

        async def fake_call_tool(session, name, arguments, max_chars):
            seen.append((name, arguments))
            if name == "check_entity_placement_batch":
                placements = json.loads(arguments["placementsJson"])
                return json.dumps(
                    {
                        "success": True,
                        "results": [
                            {
                                "id": item["id"],
                                "entity_name": item["entity_name"],
                                "x": item["x"],
                                "y": item["y"],
                                "direction": item["direction"],
                                "can_place": True,
                            }
                            for item in placements
                        ],
                    }
                )
            if name == "place_entity":
                return json.dumps({"success": True, "entity": arguments["entityName"]})
            raise AssertionError(name)

        with patch.object(clear.base, "_call_tool", fake_call_tool):
            output = await clear._build_clear_segment(
                None,
                {
                    "entityName": "transport-belt",
                    "startX": 1.5,
                    "startY": -10.5,
                    "endX": 6.5,
                    "endY": -10.5,
                },
                self._settings(),
                metrics,
                {"check_entity_placement_batch"},
            )

        payload = json.loads(output)
        self.assertTrue(payload["success"])
        self.assertEqual(payload["status"], "built")
        self.assertEqual(payload["placed_count"], 6)
        self.assertEqual(payload["direction"], "east")
        self.assertEqual(metrics.mutation_calls, 6)
        self.assertEqual(sum(1 for name, _ in seen if name == "place_entity"), 6)
        self.assertEqual(seen[0][0], "check_entity_placement_batch")

    async def test_blocked_segment_mutates_nothing_and_reports_exact_obstacle(self):
        metrics = base.RunMetrics(
            started_at=time.perf_counter(),
            plan_validated=True,
            validated_plan=self._plan(),
        )
        seen: list[str] = []

        async def fake_call_tool(session, name, arguments, max_chars):
            seen.append(name)
            if name == "check_entity_placement_batch":
                placements = json.loads(arguments["placementsJson"])
                return json.dumps(
                    {
                        "success": True,
                        "results": [
                            {
                                "id": item["id"],
                                "entity_name": item["entity_name"],
                                "x": item["x"],
                                "y": item["y"],
                                "direction": item["direction"],
                                "can_place": item["x"] != 3.5,
                            }
                            for item in placements
                        ],
                    }
                )
            if name == "get_nearby_entities":
                return json.dumps(
                    {
                        "entities": [
                            {"name": "tree-07", "type": "tree", "x": 3.5, "y": -10.5, "direction": "north"}
                        ]
                    }
                )
            if name == "place_entity":
                raise AssertionError("blocked preflight must not place anything")
            raise AssertionError(name)

        with patch.object(clear.base, "_call_tool", fake_call_tool):
            output = await clear._build_clear_segment(
                None,
                {
                    "entityName": "transport-belt",
                    "startX": 1.5,
                    "startY": -10.5,
                    "endX": 6.5,
                    "endY": -10.5,
                },
                self._settings(),
                metrics,
                {"check_entity_placement_batch"},
            )

        payload = json.loads(output)
        self.assertFalse(payload["success"])
        self.assertEqual(payload["status"], "blocked")
        self.assertTrue(payload["no_world_change"])
        self.assertEqual(payload["blocked"][0]["x"], 3.5)
        self.assertEqual(payload["blocked"][0]["existing"][0]["name"], "tree-07")
        self.assertEqual(metrics.mutation_calls, 0)
        self.assertNotIn("place_entity", seen)


class PlanPolicyRevisionTests(unittest.IsolatedAsyncioTestCase):
    async def test_old_checkpoint_is_invalidated_before_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = persistence.PersistentRunState(Path(tmp), "player", "goal")
            policy._ORIGINAL_SAVE_PLAN_CHECKPOINT(
                store,
                4,
                {"production_blocks": [], "material_routes": [], "placements": [], "removals": [], "power": {}},
                {
                    "valid": True,
                    "status": "PLAN_VALID",
                    "issue_count": 0,
                    "warning_count": 0,
                    "issues": [],
                    "warnings": [],
                },
            )
            metrics = base.RunMetrics(started_at=time.perf_counter())
            await policy._revalidate_checkpoint_policy_aware(
                None,
                None,  # stale-policy path never touches Settings or MCP
                metrics,
                set(),
                store,
                True,
            )

            raw = persistence._load_json(store.plan_path)
            self.assertEqual(raw["plan_policy_revision"], policy.CURRENT_PLAN_POLICY_REVISION)
            self.assertEqual(raw["status"], "PLAN_INVALID")
            self.assertEqual(raw["issues"][0]["code"], "planning_rate_policy_revision_changed")
            self.assertFalse(metrics.plan_validated)

    def test_checkpoint_resubmit_preserves_ai_owned_additive_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = persistence.PersistentRunState(Path(tmp), "player", "goal")
            store.save_plan_checkpoint(
                1,
                {"production_blocks": [], "material_routes": [], "placements": [], "removals": [], "power": {}},
                {"status": "PLAN_VALID", "issue_count": 0, "warning_count": 0, "issues": [], "warnings": []},
            )
            raw = persistence._load_json(store.plan_path)
            raw["owned_additive"] = {
                "1.500,-10.500": {"entity_name": "transport-belt", "x": 1.5, "y": -10.5}
            }
            persistence._write_json(store.plan_path, raw)

            store.save_plan_checkpoint(
                2,
                {"production_blocks": [], "material_routes": [], "placements": [], "removals": [], "power": {}},
                {"status": "PLAN_VALID", "issue_count": 0, "warning_count": 0, "issues": [], "warnings": []},
            )
            raw = persistence._load_json(store.plan_path)
            self.assertIn("1.500,-10.500", raw["owned_additive"])
            self.assertEqual(raw["plan_policy_revision"], policy.CURRENT_PLAN_POLICY_REVISION)


if __name__ == "__main__":
    unittest.main()
