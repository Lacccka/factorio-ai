from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from factorio_ai import app as base
from factorio_ai.persistence import PersistentRunState
from factorio_ai.plan_repair import _connected_ids, repair_power_network_disconnect
from factorio_ai.resilient_app import (
    _execute_function_calls_resilient,
    _slim_resume_bootstrap,
    _strict_resume_allowed_tool_names,
)


class DeterministicPowerRepairTests(unittest.IsolatedAsyncioTestCase):
    async def test_disconnected_medium_poles_get_live_checked_bridge(self):
        plan = {
            "production_blocks": [{"id": "ammo"}],
            "material_routes": [{"id": "iron"}],
            "placements": [
                {
                    "id": "p1",
                    "entity_name": "medium-electric-pole",
                    "x": 18.0,
                    "y": 0.0,
                    "direction": "north",
                }
            ],
            "power": {
                "pole_type": "medium-electric-pole",
                "pole_ids": ["p1"],
                "existing_anchor": {"x": 0.0, "y": 0.0},
            },
        }
        validation = {
            "status": "PLAN_INVALID",
            "issue_count": 1,
            "issues": [{"code": "power_network_disconnected", "placement_ids": ["p1"]}],
        }
        calls: list[tuple[str, dict]] = []

        async def call_mcp(name: str, arguments: dict) -> str:
            calls.append((name, arguments))
            self.assertEqual(name, "check_entity_placement_batch")
            candidates = json.loads(arguments["placementsJson"])
            return json.dumps(
                {
                    "success": True,
                    "results": [
                        {
                            "id": item["id"],
                            "entity_name": item["entity_name"],
                            "x": item["x"],
                            "y": item["y"],
                            "prototype_exists": True,
                            "can_place": True,
                        }
                        for item in candidates
                    ],
                }
            )

        repaired = await repair_power_network_disconnect(plan, validation, call_mcp)

        self.assertIsNotNone(repaired)
        assert repaired is not None
        self.assertGreater(len(repaired["power"]["pole_ids"]), 1)
        self.assertIn("p1", _connected_ids(repaired))
        self.assertTrue(calls)

    async def test_mixed_issue_set_is_not_auto_repaired(self):
        async def call_mcp(name: str, arguments: dict) -> str:
            raise AssertionError("MCP must not be called for unsupported repair")

        result = await repair_power_network_disconnect(
            {"placements": [], "power": {}},
            {
                "status": "PLAN_INVALID",
                "issues": [
                    {"code": "power_network_disconnected"},
                    {"code": "planned_entity_overlap"},
                ],
            },
            call_mcp,
        )
        self.assertIsNone(result)


class ResilientResumeGateTests(unittest.IsolatedAsyncioTestCase):
    def _metrics(self) -> base.RunMetrics:
        metrics = base.RunMetrics(started_at=0.0)
        setattr(metrics, "_resume_repair_mode", True)
        setattr(
            metrics,
            "_latest_plan_validation",
            {
                "status": "PLAN_INVALID",
                "issue_count": 1,
                "issues": [{"code": "power_network_disconnected"}],
            },
        )
        setattr(metrics, "_resume_diagnostic_calls", 0)
        return metrics

    def test_power_resume_toolset_excludes_factory_rediscovery(self):
        allowed = _strict_resume_allowed_tool_names(
            {
                "status": "PLAN_INVALID",
                "issues": [{"code": "power_network_disconnected"}],
            }
        )
        self.assertIn("get_power_network_topology", allowed)
        self.assertIn("check_entity_placement_batch", allowed)
        self.assertNotIn("survey_factory_layout", allowed)
        self.assertNotIn("scan_resources", allowed)
        self.assertNotIn("get_entity_prototype", allowed)

    def test_invalid_checkpoint_uses_compact_resume_context(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = PersistentRunState(Path(tempdir), "player-one", "Build defense hub")
            store.save_plan_checkpoint(
                4,
                {"production_blocks": [{"id": "ammo"}], "placements": []},
                {
                    "status": "PLAN_INVALID",
                    "issue_count": 1,
                    "warning_count": 0,
                    "issues": [{"code": "power_network_disconnected"}],
                    "warnings": [],
                },
            )
            context = _slim_resume_bootstrap(store, "EXPENSIVE_BOOTSTRAP", True)
            self.assertIn("ACTIVE PLAN CHECKPOINT", context)
            self.assertNotIn("EXPENSIVE_BOOTSTRAP", context)

    async def test_many_calls_in_one_response_are_blocked_after_budget(self):
        metrics = self._metrics()
        calls = [
            SimpleNamespace(
                type="function_call",
                name="get_power_network_topology",
                call_id=f"c{index}",
                arguments="{}",
            )
            for index in range(6)
        ]
        response = SimpleNamespace(output=calls)
        executed: list[str] = []

        async def fake_persistent_execute(session, single_response, settings, metrics_arg, tool_names, plan_gated):
            item = single_response.output[0]
            executed.append(item.call_id)
            metrics_arg.tool_calls += 1
            current = int(getattr(metrics_arg, "_resume_diagnostic_calls", 0) or 0)
            setattr(metrics_arg, "_resume_diagnostic_calls", current + 1)
            return [
                {
                    "type": "function_call_output",
                    "call_id": item.call_id,
                    "output": '{"status":"ok"}',
                }
            ]

        with patch(
            "factorio_ai.resilient_app._PERSISTENT_EXECUTE_FUNCTION_CALLS",
            fake_persistent_execute,
        ):
            outputs = await _execute_function_calls_resilient(
                None,
                response,
                SimpleNamespace(),
                metrics,
                set(),
                True,
            )

        self.assertEqual(executed, ["c0", "c1", "c2", "c3"])
        self.assertEqual(len(outputs), 6)
        self.assertTrue(outputs[4]["output"].startswith("RESUME_TOOL_BLOCKED:"))
        self.assertTrue(outputs[5]["output"].startswith("RESUME_TOOL_BLOCKED:"))

    async def test_unrelated_batched_tool_is_blocked_without_execution(self):
        metrics = self._metrics()
        response = SimpleNamespace(
            output=[
                SimpleNamespace(
                    type="function_call",
                    name="survey_factory_layout",
                    call_id="bad",
                    arguments="{}",
                )
            ]
        )

        async def should_not_execute(*args, **kwargs):
            raise AssertionError("unrelated resume tool must not reach MCP")

        with patch(
            "factorio_ai.resilient_app._PERSISTENT_EXECUTE_FUNCTION_CALLS",
            should_not_execute,
        ):
            outputs = await _execute_function_calls_resilient(
                None,
                response,
                SimpleNamespace(),
                metrics,
                set(),
                True,
            )

        self.assertEqual(len(outputs), 1)
        self.assertIn("RESUME_TOOL_BLOCKED", outputs[0]["output"])


if __name__ == "__main__":
    unittest.main()
