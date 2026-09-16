from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from factorio_ai import app as base
from factorio_ai import execution_budget_guard as guard


class FrozenExecutionBudgetTests(unittest.TestCase):
    def test_execution_budget_can_extend_frozen_settings(self):
        settings = base.Settings(
            provider="openai",
            player_name="player",
            rcon_host="127.0.0.1",
            rcon_port="27015",
            rcon_password="secret",
            mcp_project=Path("."),
            model="gpt-5.6-sol",
            api_key="test",
            base_url="https://api.openai.com/v1",
            max_turns=40,
            max_mutations=80,
            max_failed_mutations=8,
            tool_result_max_chars=20000,
        )
        metrics = SimpleNamespace(plan_validated=True)

        with patch.dict("os.environ", {"AGENT_EXECUTION_EXTRA_TURNS": "30"}):
            guard._extend_execution_turn_budget(settings, metrics)
            guard._extend_execution_turn_budget(settings, metrics)

        self.assertEqual(settings.max_turns, 70)
        self.assertTrue(metrics._execution_turn_budget_extended)


if __name__ == "__main__":
    unittest.main()
