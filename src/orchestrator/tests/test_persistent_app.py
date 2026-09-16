from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from factorio_ai import app as base
from factorio_ai.persistence import PersistentRunState
from factorio_ai.persistent_app import (
    DEEPSEEK_HISTORY_MAX_GROUPS,
    MAX_FORCED_PLAN_CONTINUATIONS,
    RESUME_DIAGNOSTIC_TOOL_BUDGET,
    _active_tools_persistent,
    _checkpoint_for_run,
    _deepseek_history_input,
    _openai_extra_body,
    _should_force_plan_continue,
    _slim_live_bootstrap,
)
from factorio_ai.planning import PLAN_TOOL_NAME, PLAN_TOOL_SCHEMA


class PersistentPlanGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = PersistentRunState(Path(self.tempdir.name), "player-one", "Build defense hub")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _metrics(self) -> base.RunMetrics:
        return base.RunMetrics(started_at=0.0)

    def _tool(self, name: str) -> dict:
        return {
            "type": "function",
            "name": name,
            "description": "",
            "parameters": {"type": "object", "properties": {}},
        }

    def test_invalid_checkpoint_seeds_attempt_counter_and_forces_continuation(self):
        self.store.save_plan_checkpoint(
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
        metrics = self._metrics()

        checkpoint = _checkpoint_for_run(self.store, metrics, True)

        self.assertIsNotNone(checkpoint)
        self.assertEqual(metrics.plan_validation_attempts, 4)
        self.assertTrue(_should_force_plan_continue(True, metrics, self.store, 0))

    def test_valid_plan_or_retry_cap_allows_completion(self):
        self.store.save_plan_checkpoint(
            2,
            {"production_blocks": [{"id": "ammo"}], "placements": []},
            {
                "status": "PLAN_INVALID",
                "issue_count": 1,
                "warning_count": 0,
                "issues": [{"code": "entity_without_power_coverage"}],
                "warnings": [],
            },
        )
        metrics = self._metrics()
        _checkpoint_for_run(self.store, metrics, True)

        self.assertFalse(
            _should_force_plan_continue(
                True,
                metrics,
                self.store,
                MAX_FORCED_PLAN_CONTINUATIONS,
            )
        )

        metrics.plan_validated = True
        self.assertFalse(_should_force_plan_continue(True, metrics, self.store, 0))

    def test_normal_non_gated_run_is_never_forced(self):
        metrics = self._metrics()
        setattr(metrics, "_latest_plan_validation", {"status": "PLAN_INVALID", "issue_count": 3})
        self.assertFalse(_should_force_plan_continue(False, metrics, self.store, 0))

    def test_resumed_power_issue_hides_broad_discovery_tools(self):
        self.store.save_plan_checkpoint(
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
        metrics = self._metrics()
        _checkpoint_for_run(self.store, metrics, True)

        read_only = [
            self._tool("survey_factory_layout"),
            self._tool("scan_resources"),
            self._tool("get_power_network_topology"),
            self._tool("check_entity_placement_batch"),
            self._tool("get_nearby_entities"),
        ]
        gated = [*read_only, PLAN_TOOL_SCHEMA]
        active = _active_tools_persistent(gated, gated, read_only, metrics, True)
        names = {tool["name"] for tool in active}

        self.assertIn(PLAN_TOOL_NAME, names)
        self.assertIn("get_power_network_topology", names)
        self.assertIn("check_entity_placement_batch", names)
        self.assertNotIn("survey_factory_layout", names)
        self.assertNotIn("scan_resources", names)

    def test_resume_diagnostic_budget_forces_plan_submit(self):
        self.store.save_plan_checkpoint(
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
        metrics = self._metrics()
        _checkpoint_for_run(self.store, metrics, True)
        setattr(metrics, "_resume_diagnostic_calls", RESUME_DIAGNOSTIC_TOOL_BUDGET)

        read_only = [self._tool("get_power_network_topology")]
        gated = [*read_only, PLAN_TOOL_SCHEMA]
        active = _active_tools_persistent(gated, gated, read_only, metrics, True)

        self.assertEqual([tool["name"] for tool in active], [PLAN_TOOL_NAME])

    def test_persisted_valid_plan_exposes_only_submit_until_fresh_validation(self):
        self.store.save_plan_checkpoint(
            5,
            {"production_blocks": [{"id": "ammo"}], "placements": []},
            {
                "status": "PLAN_VALID",
                "issue_count": 0,
                "warning_count": 0,
                "issues": [],
                "warnings": [],
            },
        )
        metrics = self._metrics()
        _checkpoint_for_run(self.store, metrics, True)

        read_only = [self._tool("get_power_network_topology")]
        gated = [*read_only, PLAN_TOOL_SCHEMA]
        active = _active_tools_persistent(gated, gated, read_only, metrics, True)

        self.assertEqual([tool["name"] for tool in active], [PLAN_TOOL_NAME])

    def test_deepseek_history_keeps_only_recent_atomic_groups(self):
        initial = {"role": "user", "content": "bootstrap"}
        groups = [
            [
                {"type": "function_call", "call_id": f"c{index}", "name": "tool", "arguments": "{}"},
                {"type": "function_call_output", "call_id": f"c{index}", "output": f"result-{index}"},
            ]
            for index in range(DEEPSEEK_HISTORY_MAX_GROUPS + 2)
        ]

        history = _deepseek_history_input(initial, groups)
        rendered = str(history)

        self.assertEqual(history[0], initial)
        self.assertNotIn("result-0", rendered)
        self.assertNotIn("result-1", rendered)
        self.assertIn(f"result-{DEEPSEEK_HISTORY_MAX_GROUPS + 1}", rendered)
        # Each retained output must still have its matching function call.
        for item in history[1:]:
            if item.get("type") == "function_call_output":
                call_id = item["call_id"]
                self.assertTrue(
                    any(
                        other.get("type") == "function_call" and other.get("call_id") == call_id
                        for other in history[1:]
                    )
                )

    def test_warm_bootstrap_keeps_only_volatile_live_sections(self):
        bootstrap = """### get_player_position
{\"x\":1}

### get_inventory_summary
{\"iron\":2}

### get_existing_factory_summary
EXPENSIVE_FACTORY_SUMMARY

### survey_factory_layout
EXPENSIVE_LAYOUT

### get_electric_network
{\"satisfaction_percent\":100}"""

        compact = _slim_live_bootstrap(bootstrap)

        self.assertIn("get_player_position", compact)
        self.assertIn("get_inventory_summary", compact)
        self.assertIn("get_electric_network", compact)
        self.assertNotIn("EXPENSIVE_FACTORY_SUMMARY", compact)
        self.assertNotIn("EXPENSIVE_LAYOUT", compact)

    def test_openai_requests_use_stable_cache_key_and_auto_compaction(self):
        settings = SimpleNamespace(player_name="player-one", model="gpt-5.6-terra")
        with patch.dict("os.environ", {"OPENAI_CONTEXT_COMPACT_TOKENS": "64000"}):
            body = _openai_extra_body(settings, True)

        self.assertTrue(str(body["prompt_cache_key"]).startswith("factorio-ai-"))
        self.assertEqual(
            body["context_management"],
            [{"type": "compaction", "compact_threshold": 64000}],
        )


if __name__ == "__main__":
    unittest.main()
