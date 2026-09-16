from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from factorio_ai import app as base
from factorio_ai import survey_app


class SurveyOnlyRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def test_active_tools_remove_mutations_and_plan_submission(self):
        metrics = base.RunMetrics(started_at=0.0)
        tools = [
            {"type": "function", "name": "get_player_position", "parameters": {}},
            {"type": "function", "name": "survey_factory_layout", "parameters": {}},
            {"type": "function", "name": "walk_to_position", "parameters": {}},
            {"type": "function", "name": "place_entity", "parameters": {}},
            {"type": "function", "name": "craft", "parameters": {}},
            {"type": "function", "name": "submit_factory_plan", "parameters": {}},
        ]
        read_only = [
            tool
            for tool in tools
            if tool["name"] not in {"place_entity", "craft", "submit_factory_plan"}
        ]

        visible = survey_app._survey_active_tools(
            tools,
            read_only,
            read_only,
            metrics,
            False,
        )
        names = {tool["name"] for tool in visible}

        self.assertIn("get_player_position", names)
        self.assertIn("survey_factory_layout", names)
        self.assertIn("walk_to_position", names)
        self.assertNotIn("place_entity", names)
        self.assertNotIn("craft", names)
        self.assertNotIn("submit_factory_plan", names)

    async def test_hallucinated_mutation_is_blocked_before_dispatch(self):
        metrics = base.RunMetrics(started_at=0.0)
        response = SimpleNamespace(
            output=[
                SimpleNamespace(
                    type="function_call",
                    name="place_entity",
                    call_id="blocked",
                    arguments='{"entityName":"transport-belt","x":0,"y":0}',
                )
            ]
        )

        async def should_not_execute(*args, **kwargs):
            raise AssertionError("survey-only mutation must never reach MCP")

        with patch(
            "factorio_ai.survey_app._PRIOR_EXECUTE_FUNCTION_CALLS",
            should_not_execute,
        ):
            outputs = await survey_app._execute_function_calls_survey_only(
                None,
                response,
                SimpleNamespace(),
                metrics,
                {"place_entity"},
                False,
            )

        self.assertEqual(len(outputs), 1)
        self.assertTrue(outputs[0]["output"].startswith("SURVEY_ONLY_TOOL_BLOCKED:"))
        self.assertEqual(metrics.blocked_mutations, 1)


if __name__ == "__main__":
    unittest.main()
