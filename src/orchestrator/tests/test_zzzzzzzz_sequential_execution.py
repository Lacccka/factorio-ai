from __future__ import annotations

import json
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from factorio_ai import app as base
from factorio_ai import sequential_execution as sequential


class SequentialExecutionTests(unittest.IsolatedAsyncioTestCase):
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
    def _call(call_id: str, x: float) -> SimpleNamespace:
        return SimpleNamespace(
            type="function_call",
            name="place_entity",
            call_id=call_id,
            arguments=json.dumps(
                {"entityName": "transport-belt", "x": x, "y": -290.5, "direction": "west"}
            ),
        )

    async def test_real_collision_defers_later_mutations_from_same_response(self):
        calls: list[str] = []

        async def prior(session, response, settings, metrics, tool_names, plan_gated):
            item = response.output[0]
            calls.append(item.call_id)
            metrics.tool_calls += 1
            metrics.mutation_calls += 1
            return [
                {
                    "type": "function_call_output",
                    "call_id": item.call_id,
                    "output": json.dumps({"success": False, "error": "invalid_position"}),
                }
            ]

        response = SimpleNamespace(output=[self._call("a", -18.5), self._call("b", -17.5)])
        metrics = base.RunMetrics(started_at=time.perf_counter(), plan_validated=True)
        with patch.object(sequential, "_PRIOR_EXECUTE_FUNCTION_CALLS", prior):
            outputs = await sequential._execute_function_calls_sequentially(
                object(),
                response,
                self._settings(),
                metrics,
                {"place_entity", "walk_to_position"},
                True,
            )

        self.assertEqual(calls, ["a"])
        self.assertIn("invalid_position", outputs[0]["output"])
        self.assertIn("EXECUTION_DEFERRED", outputs[1]["output"])
        self.assertEqual(metrics.mutation_calls, 1)
        self.assertEqual(metrics.blocked_mutations, 1)

    async def test_out_of_range_is_walked_and_retried_without_consuming_safety_budget(self):
        attempts: dict[str, int] = {}

        async def prior(session, response, settings, metrics, tool_names, plan_gated):
            item = response.output[0]
            attempts[item.call_id] = attempts.get(item.call_id, 0) + 1
            metrics.tool_calls += 1
            metrics.mutation_calls += 1
            if item.call_id == "a" and attempts[item.call_id] == 1:
                output = json.dumps({"success": False, "error": "out_of_range"})
            else:
                output = json.dumps({"success": True, "entity": "transport-belt"})
            return [{"type": "function_call_output", "call_id": item.call_id, "output": output}]

        async def call_tool(session, name, arguments, max_chars):
            self.assertEqual(name, "walk_to_position")
            return json.dumps({"status": "arrived", "x": arguments["targetX"], "y": arguments["targetY"]})

        response = SimpleNamespace(output=[self._call("a", -70.5), self._call("b", -69.5)])
        metrics = base.RunMetrics(started_at=time.perf_counter(), plan_validated=True)
        with (
            patch.object(sequential, "_PRIOR_EXECUTE_FUNCTION_CALLS", prior),
            patch.object(base, "_call_tool", call_tool),
        ):
            outputs = await sequential._execute_function_calls_sequentially(
                object(),
                response,
                self._settings(),
                metrics,
                {"place_entity", "walk_to_position"},
                True,
            )

        self.assertEqual(attempts, {"a": 2, "b": 1})
        self.assertTrue(all(json.loads(item["output"])["success"] for item in outputs))
        # Three placement attempts occurred, but the pure range miss is refunded.
        self.assertEqual(metrics.mutation_calls, 2)


if __name__ == "__main__":
    unittest.main()
