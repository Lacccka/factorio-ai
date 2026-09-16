from __future__ import annotations

import unittest
from types import SimpleNamespace

from factorio_ai.phase_tools import filter_tools_for_phase, phase_for


def _tool(name: str) -> dict:
    return {
        "type": "function",
        "name": name,
        "description": "",
        "parameters": {"type": "object", "properties": {}},
    }


def _is_mutating(name: str) -> bool:
    return name.startswith(("place_", "set_", "mine_", "research_", "attack_")) or name == "craft"


class PhaseToolTests(unittest.TestCase):
    def test_plan_phase_hides_world_mutations_and_unrelated_tools(self):
        tools = [
            _tool("survey_factory_layout"),
            _tool("get_recipe_details"),
            _tool("check_entity_placement_batch"),
            _tool("submit_factory_plan"),
            _tool("place_entity"),
            _tool("attack_nearest_enemy"),
            _tool("drive_vehicle"),
        ]
        filtered = filter_tools_for_phase(tools, "plan", _is_mutating)
        names = {tool["name"] for tool in filtered}
        self.assertIn("survey_factory_layout", names)
        self.assertIn("submit_factory_plan", names)
        self.assertNotIn("place_entity", names)
        self.assertNotIn("attack_nearest_enemy", names)
        self.assertNotIn("drive_vehicle", names)

    def test_execution_phase_keeps_build_tools_and_local_verification(self):
        tools = [
            _tool("place_entity"),
            _tool("set_assembler_recipe"),
            _tool("craft"),
            _tool("walk_to_position"),
            _tool("inspect_entity"),
            _tool("get_electric_network"),
            _tool("submit_factory_plan"),
            _tool("survey_factory_layout"),
            _tool("attack_nearest_enemy"),
        ]
        filtered = filter_tools_for_phase(tools, "execute", _is_mutating)
        names = {tool["name"] for tool in filtered}
        self.assertIn("place_entity", names)
        self.assertIn("set_assembler_recipe", names)
        self.assertIn("craft", names)
        self.assertIn("walk_to_position", names)
        self.assertIn("inspect_entity", names)
        self.assertIn("submit_factory_plan", names)
        self.assertNotIn("survey_factory_layout", names)
        self.assertNotIn("attack_nearest_enemy", names)

    def test_verify_phase_removes_mutations(self):
        tools = [
            _tool("inspect_entity"),
            _tool("get_power_network_topology"),
            _tool("place_entity"),
            _tool("craft"),
        ]
        filtered = filter_tools_for_phase(tools, "verify", _is_mutating)
        self.assertEqual(
            {tool["name"] for tool in filtered},
            {"inspect_entity", "get_power_network_topology"},
        )

    def test_resume_phase_preserves_upstream_issue_scoped_surface(self):
        tools = [_tool("get_power_network_topology"), _tool("submit_factory_plan")]
        self.assertEqual(filter_tools_for_phase(tools, "resume", _is_mutating), tools)

    def test_phase_selection(self):
        metrics = SimpleNamespace(
            mutation_budget_exhausted=False,
            plan_validated=False,
            _resume_repair_mode=False,
        )
        self.assertEqual(phase_for(metrics, True), "plan")
        metrics._resume_repair_mode = True
        self.assertEqual(phase_for(metrics, True), "resume")
        metrics.plan_validated = True
        self.assertEqual(phase_for(metrics, True), "execute")
        metrics.mutation_budget_exhausted = True
        self.assertEqual(phase_for(metrics, True), "verify")
        self.assertEqual(phase_for(metrics, False), "general")


if __name__ == "__main__":
    unittest.main()
