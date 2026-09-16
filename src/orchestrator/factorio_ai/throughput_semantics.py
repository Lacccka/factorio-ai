from __future__ import annotations

import json
import math
from typing import Any, Awaitable, Callable

from . import app as base
from . import resilient_app as resilient
from .planning import BELT_CAPACITY_ITEMS_PER_SECOND, PLAN_TOOL_SCHEMA


CallMcp = Callable[[str, dict[str, Any]], Awaitable[str]]
_PRIOR_VALIDATE_FACTORY_PLAN = base.validate_factory_plan
_PROMPT_MARKER = "DESIGN THROUGHPUT SEMANTICS"


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_json(output: str) -> dict[str, Any] | None:
    text = output.strip()
    if text.startswith(("MCP_TOOL_ERROR:", "MCP_TOOL_EXCEPTION:")):
        return None
    try:
        value = json.loads(text)
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def _finish(validation: dict[str, Any]) -> dict[str, Any]:
    issues = [item for item in (validation.get("issues") or []) if isinstance(item, dict)]
    warnings = [item for item in (validation.get("warnings") or []) if isinstance(item, dict)]
    validation["issues"] = issues
    validation["warnings"] = warnings
    validation["issue_count"] = len(issues)
    validation["warning_count"] = len(warnings)
    validation["valid"] = not issues
    validation["status"] = "PLAN_VALID" if not issues else "PLAN_INVALID"
    if not issues:
        validation["message"] = (
            "Plan architecture passed deterministic validation. target_rate_per_second is the sustained design throughput; "
            "machine maximum speed is capacity headroom, not required upstream flow. Mutating tools may be unlocked."
        )
    return validation


def _install_schema_description() -> None:
    try:
        field = (
            PLAN_TOOL_SCHEMA["parameters"]["properties"]["plan"]["properties"]
            ["production_blocks"]["items"]["properties"]["target_rate_per_second"]
        )
    except (KeyError, TypeError):
        return
    field["description"] = (
        "Sustained design output rate for target_item. When set, the validator sizes input/output/fuel routes to this rate, "
        "not to the machine block's theoretical maximum. machine_count only has to provide enough capacity."
    )


_install_schema_description()


async def _validate_factory_plan_design_throughput(
    plan: Any,
    call_mcp: CallMcp,
    available_tool_names: set[str],
) -> dict[str, Any]:
    validation = await _PRIOR_VALIDATE_FACTORY_PLAN(plan, call_mcp, available_tool_names)
    if not isinstance(validation, dict) or not isinstance(plan, dict):
        return validation

    blocks = [item for item in (plan.get("production_blocks") or []) if isinstance(item, dict)]
    if not blocks:
        return validation

    summaries = {
        str(item.get("id", "")): item
        for item in (validation.get("production_blocks") or [])
        if isinstance(item, dict) and str(item.get("id", ""))
    }

    block_inputs: dict[tuple[str, str], float] = {}
    block_outputs: dict[tuple[str, str], float] = {}
    block_utilization: dict[str, float] = {}
    block_fuel_full_rate: dict[str, float] = {}

    for block in blocks:
        block_id = str(block.get("id", "")).strip()
        summary = summaries.get(block_id)
        if not block_id or summary is None:
            continue

        recipe_name = str(block.get("recipe", "")).strip()
        if not recipe_name:
            continue
        recipe = _parse_json(await call_mcp("get_recipe_details", {"recipe": recipe_name}))
        if not recipe or recipe.get("success") is False:
            continue

        capacity_cycles = _number(summary.get("cycles_per_second"))
        if capacity_cycles <= 0:
            continue
        target_item = str(block.get("target_item") or summary.get("target_item") or "").strip()
        target_rate = _number(block.get("target_rate_per_second"))
        capacity_target_rate = _number(summary.get("actual_output_rate_per_second"))

        utilization = 1.0
        if target_rate > 0 and capacity_target_rate > 0:
            utilization = min(1.0, target_rate / capacity_target_rate)
        block_utilization[block_id] = utilization
        design_cycles = capacity_cycles * utilization

        for ingredient in recipe.get("ingredients", []) or []:
            if not isinstance(ingredient, dict) or ingredient.get("type", "item") != "item":
                continue
            item = str(ingredient.get("name", ""))
            if item:
                block_inputs[(block_id, item)] = block_inputs.get((block_id, item), 0.0) + _number(ingredient.get("amount")) * design_cycles

        for product in recipe.get("products", []) or []:
            if not isinstance(product, dict) or product.get("type", "item") != "item":
                continue
            item = str(product.get("name", ""))
            if not item:
                continue
            probability = _number(product.get("probability"), 1.0) or 1.0
            block_outputs[(block_id, item)] = block_outputs.get((block_id, item), 0.0) + _number(product.get("amount")) * probability * design_cycles

        summary["capacity_output_rate_per_second"] = round(capacity_target_rate, 6)
        summary["planned_output_rate_per_second"] = round(
            block_outputs.get((block_id, target_item), capacity_target_rate * utilization),
            6,
        )
        summary["design_utilization"] = round(utilization, 6)

        machine_count = int(block.get("machine_count") or 0)
        if target_rate > 0 and capacity_target_rate > 0 and machine_count > 0:
            per_machine = capacity_target_rate / machine_count
            minimum = max(1, int(math.ceil((target_rate - 1e-12) / per_machine)))
            if machine_count > minimum:
                validation.setdefault("warnings", []).append(
                    {
                        "code": "production_capacity_headroom",
                        "message": (
                            f"Block {block_id} uses {machine_count} machines but only {minimum} are required for the declared "
                            f"design rate {target_rate:.3f}/s. Do not expand upstream merely to feed theoretical maximum capacity."
                        ),
                        "block_id": block_id,
                        "machine_count": machine_count,
                        "minimum_machine_count": minimum,
                        "target_rate_per_second": round(target_rate, 6),
                        "capacity_rate_per_second": round(capacity_target_rate, 6),
                    }
                )

        machine = str(block.get("machine", "")).strip()
        if machine and "get_entity_prototype" in available_tool_names:
            prototype = _parse_json(await call_mcp("get_entity_prototype", {"entityName": machine}))
            if prototype and prototype.get("has_burner") is True:
                energy_usage = _number(prototype.get("energy_usage"))
                effectivity = max(_number(prototype.get("burner_effectivity"), 1.0), 1e-9)
                if machine_count > 0 and energy_usage > 0:
                    # Full-load joules/tick. The route-specific fuel value is applied below.
                    block_fuel_full_rate[block_id] = machine_count * energy_usage * 60.0 / effectivity

    route_summaries = {
        str(item.get("id", "")): item
        for item in (validation.get("material_routes") or [])
        if isinstance(item, dict) and str(item.get("id", ""))
    }
    desired_route_rates: dict[str, float] = {}

    for route in plan.get("material_routes", []) or []:
        if not isinstance(route, dict):
            continue
        route_id = str(route.get("id", "")).strip()
        item = str(route.get("item", "")).strip()
        role = str(route.get("role", "")).strip().lower()
        if not route_id or not item:
            continue

        required = 0.0
        if role == "input":
            for block_id in route.get("feeds_blocks", []) or []:
                required += block_inputs.get((str(block_id), item), 0.0)
        elif role == "output":
            for block_id in route.get("source_blocks", []) or []:
                required += block_outputs.get((str(block_id), item), 0.0)
        elif role == "fuel" and "get_item_fuel_info" in available_tool_names:
            fuel = _parse_json(await call_mcp("get_item_fuel_info", {"itemName": item}))
            fuel_value = 0.0 if not fuel else _number(fuel.get("fuel_value"))
            if fuel_value > 0:
                for block_id_raw in route.get("feeds_blocks", []) or []:
                    block_id = str(block_id_raw)
                    joules_per_second = block_fuel_full_rate.get(block_id, 0.0)
                    required += joules_per_second * block_utilization.get(block_id, 1.0) / fuel_value
        else:
            continue

        if required <= 0:
            continue
        desired_route_rates[route_id] = required
        summary = route_summaries.get(route_id)
        if summary is not None:
            summary["required_rate_per_second"] = round(required, 6)
            summary["rate_basis"] = "target_rate_per_second"

    # The legacy validator sizes belt capacity to 100% machine utilization. Replace only
    # those capacity failures whose declared design throughput actually fits the route.
    filtered_issues: list[dict[str, Any]] = []
    for issue in validation.get("issues", []) or []:
        if not isinstance(issue, dict) or issue.get("code") != "belt_capacity_exceeded":
            filtered_issues.append(issue)
            continue
        route_id = str(issue.get("route_id", ""))
        desired = desired_route_rates.get(route_id)
        route_summary = route_summaries.get(route_id)
        capacity = _number((route_summary or {}).get("capacity_per_second"))
        if desired is not None and capacity > 0 and desired <= capacity + 1e-9:
            continue
        filtered_issues.append(issue)
    validation["issues"] = filtered_issues

    return _finish(validation)


base.validate_factory_plan = _validate_factory_plan_design_throughput
resilient.validate_factory_plan = _validate_factory_plan_design_throughput

if _PROMPT_MARKER not in base.SYSTEM_PROMPT:
    base.SYSTEM_PROMPT += """

DESIGN THROUGHPUT SEMANTICS:
target_rate_per_second is the sustained design throughput, not an instruction to feed every machine at its theoretical maximum. Size ingredient, output and burner-fuel routes to the declared target rate while ensuring machine_count has enough capacity. Capacity headroom is allowed. Never expand an upstream mining/smelting district solely because a downstream machine could consume much faster than the requested rate. Before adding upstream machines, compare the target demand with the live capacity of the existing district.
"""
