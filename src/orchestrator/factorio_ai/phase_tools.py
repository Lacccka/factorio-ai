from __future__ import annotations

from typing import Any, Callable

from .planning import PLAN_TOOL_NAME


_PLANNING_TOOLS = {
    PLAN_TOOL_NAME,
    "get_player_position",
    "get_inventory_summary",
    "get_research_status",
    "get_researched_technologies",
    "get_available_technologies",
    "get_existing_factory_summary",
    "survey_factory_layout",
    "summarize_area",
    "scan_resources",
    "get_power_network_topology",
    "get_electric_network",
    "find_buildable_area",
    "get_nearby_entities",
    "get_area_occupancy",
    "inspect_entity",
    "inspect_entity_multiple",
    "get_entity_prototype",
    "get_recipe_details",
    "calculate_production_rate",
    "get_item_fuel_info",
    "trace_item_flow",
    "get_flow_graph",
    "check_entity_placement_batch",
    "plan_power_poles",
    "find_nearest_power_pole",
    "count_item_in_world",
    "check_craft_feasibility",
    "find_idle_machines",
}

_VERIFY_TOOLS = {
    PLAN_TOOL_NAME,
    "get_player_position",
    "get_inventory_summary",
    "get_research_status",
    "get_electric_network",
    "get_power_network_topology",
    "get_nearby_entities",
    "get_area_occupancy",
    "inspect_entity",
    "inspect_entity_multiple",
    "trace_item_flow",
    "get_flow_graph",
    "count_item_in_world",
    "find_idle_machines",
    "check_entity_placement_batch",
    "emergency_stop",
}

_EXECUTION_READ_ONLY_TOOLS = _VERIFY_TOOLS | {
    "get_recipe_details",
    "get_entity_prototype",
    "check_craft_feasibility",
    "find_nearest_power_pole",
}

_EXECUTION_EXACT_TOOLS = {
    "craft",
    "ensure_item",
    "clear_remnants",
    "walk_to_position",
    "safe_walk_to_position",
    "emergency_stop",
}

_EXECUTION_MUTATION_PREFIXES = (
    "place_",
    "mine_",
    "rotate_",
    "set_",
    "connect_",
    "disconnect_",
    "insert_",
    "remove_",
    "pickup_",
    "drop_",
    "transfer_",
    "repair_",
    "refuel_",
    "research_",
    "start_research",
    "cancel_research",
    "revive_",
)


def tool_name(tool: dict[str, Any]) -> str:
    return str(tool.get("name", ""))


def phase_for(metrics: Any, plan_gated: bool) -> str:
    if not plan_gated:
        return "general"
    if bool(getattr(metrics, "mutation_budget_exhausted", False)):
        return "verify"
    if not bool(getattr(metrics, "plan_validated", False)):
        if bool(getattr(metrics, "_resume_repair_mode", False)):
            return "resume"
        return "plan"
    return "execute"


def _execution_allowed(name: str, is_mutating_tool: Callable[[str], bool]) -> bool:
    if name in _EXECUTION_READ_ONLY_TOOLS or name in _EXECUTION_EXACT_TOOLS:
        return True
    if name == PLAN_TOOL_NAME:
        return True
    if not is_mutating_tool(name):
        return False
    return name.startswith(_EXECUTION_MUTATION_PREFIXES)


def filter_tools_for_phase(
    tools: list[dict[str, Any]],
    phase: str,
    is_mutating_tool: Callable[[str], bool],
) -> list[dict[str, Any]]:
    """Return the smallest stable tool surface appropriate for the current agent phase.

    The goal is not just token reduction. A smaller action space prevents the model from
    reopening unrelated discovery or operational branches after a plan has already settled.
    Resume repair is already issue-scoped by persistent_app, so it is intentionally left alone.
    """
    if phase in {"general", "resume"}:
        return tools
    if phase == "plan":
        filtered = [tool for tool in tools if tool_name(tool) in _PLANNING_TOOLS]
    elif phase == "verify":
        filtered = [tool for tool in tools if tool_name(tool) in _VERIFY_TOOLS]
    elif phase == "execute":
        filtered = [tool for tool in tools if _execution_allowed(tool_name(tool), is_mutating_tool)]
    else:
        return tools

    # Do not return an empty surface if an upstream MCP revision renamed every helper.
    # Falling back is safer than silently making a task impossible.
    return filtered or tools
