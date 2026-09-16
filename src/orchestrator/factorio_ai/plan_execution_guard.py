from __future__ import annotations

import json
import math
import sys
from types import SimpleNamespace
from typing import Any, Awaitable, Callable

from . import app as base
from . import resilient_app as resilient
from .planning import PLAN_TOOL_NAME, PLAN_TOOL_SCHEMA


CallMcp = Callable[[str, dict[str, Any]], Awaitable[str]]

_PRIOR_VALIDATE_FACTORY_PLAN = base.validate_factory_plan
_PRIOR_EXECUTE_FUNCTION_CALLS = base._execute_function_calls
_PROMPT_MARKER = "VALIDATED EXECUTION CONFORMANCE POLICY"


def _install_plan_schema_extensions() -> None:
    plan_schema = PLAN_TOOL_SCHEMA["parameters"]["properties"]["plan"]
    properties = plan_schema["properties"]
    required = plan_schema.setdefault("required", [])

    route_properties = properties["material_routes"]["items"]["properties"]
    route_properties.setdefault(
        "tap_placement_id",
        {
            "type": "string",
            "description": (
                "For source_mode=tap, id of the planned splitter placement that implements "
                "the tap. The splitter must be fully preflighted and may not consume an adjacent bus lane."
            ),
        },
    )

    properties.setdefault(
        "removals",
        {
            "type": "array",
            "description": (
                "Exact existing entities that the validated plan is allowed to remove before replacement. "
                "Execution may not mine arbitrary factory entities outside this list."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "entity_name": {"type": "string"},
                    "x": {"type": "number"},
                    "y": {"type": "number"},
                    "replaced_by": {
                        "type": "string",
                        "description": "Optional planned placement id that replaces this entity.",
                    },
                    "reason": {"type": "string"},
                },
                "required": ["id", "entity_name", "x", "y"],
            },
        },
    )
    if "removals" not in required:
        required.append("removals")


_install_plan_schema_extensions()


def _parse_json(output: str) -> dict[str, Any] | None:
    text = output.split("\n\nMUTATION_BUDGET_REACHED:", 1)[0].strip()
    if text.startswith(("MCP_TOOL_ERROR:", "MCP_TOOL_EXCEPTION:", "INVALID_TOOL_ARGUMENTS:")):
        return None
    try:
        value = json.loads(text)
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _same(a: float | None, b: float | None, tolerance: float = 0.11) -> bool:
    return a is not None and b is not None and abs(a - b) <= tolerance


def _arguments(item: Any) -> dict[str, Any]:
    try:
        value = json.loads(getattr(item, "arguments", "") or "{}")
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _plan_placements(plan: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in (plan.get("placements") or []) if isinstance(item, dict)]


def _plan_removals(plan: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in (plan.get("removals") or []) if isinstance(item, dict)]


def _placement_by_id(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("id", "")): item
        for item in _plan_placements(plan)
        if str(item.get("id", ""))
    }


def _production_by_id(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("id", "")): item
        for item in (plan.get("production_blocks") or [])
        if isinstance(item, dict) and str(item.get("id", ""))
    }


def _matching_live_entity(
    payload: dict[str, Any] | None,
    x: float,
    y: float,
    entity_name: str | None = None,
) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    for entity in payload.get("entities", []) or []:
        if not isinstance(entity, dict):
            continue
        ex = _number(entity.get("x"))
        ey = _number(entity.get("y"))
        if not _same(ex, x) or not _same(ey, y):
            continue
        if entity_name and str(entity.get("name", "")) != entity_name:
            continue
        return entity
    return None


async def _nearby(call_mcp: CallMcp, x: float, y: float, radius: float = 1.5) -> dict[str, Any] | None:
    return _parse_json(
        await call_mcp(
            "get_nearby_entities",
            {"radius": radius, "centerX": x, "centerY": y},
        )
    )


def _splitter_lane_centers(placement: dict[str, Any]) -> tuple[tuple[float, float], tuple[float, float]] | None:
    x = _number(placement.get("x"))
    y = _number(placement.get("y"))
    if x is None or y is None:
        return None
    direction = str(placement.get("direction", "north")).lower()
    if direction in {"north", "south"}:
        return ((x - 0.5, y), (x + 0.5, y))
    if direction in {"east", "west"}:
        return ((x, y - 0.5), (x, y + 0.5))
    return None


def _lane_for_source(
    placement: dict[str, Any],
    source_x: float,
    source_y: float,
) -> tuple[tuple[float, float], tuple[float, float]] | None:
    lanes = _splitter_lane_centers(placement)
    if lanes is None:
        return None
    first, second = lanes
    if _same(first[0], source_x) and _same(first[1], source_y):
        return first, second
    if _same(second[0], source_x) and _same(second[1], source_y):
        return second, first
    return None


def _append_issue(validation: dict[str, Any], code: str, message: str, **extra: Any) -> None:
    issues = validation.setdefault("issues", [])
    if not isinstance(issues, list):
        issues = []
        validation["issues"] = issues
    issues.append({"code": code, "message": message, **extra})


def _append_warning(validation: dict[str, Any], code: str, message: str, **extra: Any) -> None:
    warnings = validation.setdefault("warnings", [])
    if not isinstance(warnings, list):
        warnings = []
        validation["warnings"] = warnings
    warnings.append({"code": code, "message": message, **extra})


def _finish_validation(validation: dict[str, Any]) -> dict[str, Any]:
    issues = [item for item in (validation.get("issues") or []) if isinstance(item, dict)]
    warnings = [item for item in (validation.get("warnings") or []) if isinstance(item, dict)]
    validation["issues"] = issues
    validation["warnings"] = warnings
    validation["issue_count"] = len(issues)
    validation["warning_count"] = len(warnings)
    validation["valid"] = not issues
    validation["status"] = "PLAN_VALID" if not issues else "PLAN_INVALID"
    validation["message"] = (
        "Plan passed deterministic validation, including exact tap replacement geometry and execution authorization."
        if not issues
        else "Plan failed deterministic validation. Correct the listed issues and resubmit before any mutation."
    )
    return validation


async def _validate_factory_plan_conformant(
    plan: Any,
    call_mcp: CallMcp,
    available_tool_names: set[str],
) -> dict[str, Any]:
    validation = await _PRIOR_VALIDATE_FACTORY_PLAN(plan, call_mcp, available_tool_names)
    if not isinstance(validation, dict):
        validation = {"valid": False, "status": "PLAN_INVALID", "issues": [], "warnings": []}
    if not isinstance(plan, dict):
        return _finish_validation(validation)

    removals_raw = plan.get("removals")
    if not isinstance(removals_raw, list):
        _append_issue(
            validation,
            "missing_removals",
            "Plan must include removals (use an empty array when nothing is intentionally removed).",
        )
        removals: list[dict[str, Any]] = []
    else:
        removals = [item for item in removals_raw if isinstance(item, dict)]

    removal_ids: set[str] = set()
    valid_removals: list[dict[str, Any]] = []
    placements = _placement_by_id(plan)

    for removal in removals:
        removal_id = str(removal.get("id", "")).strip()
        entity_name = str(removal.get("entity_name", "")).strip()
        x = _number(removal.get("x"))
        y = _number(removal.get("y"))
        if not removal_id or removal_id in removal_ids:
            _append_issue(
                validation,
                "invalid_removal_id",
                f"Removal id must be unique and non-empty: {removal_id!r}",
                removal_id=removal_id,
            )
            continue
        removal_ids.add(removal_id)
        if not entity_name or x is None or y is None:
            _append_issue(
                validation,
                "invalid_removal",
                f"Removal {removal_id} requires entity_name, x and y.",
                removal_id=removal_id,
            )
            continue
        replacement_id = str(removal.get("replaced_by", "")).strip()
        if replacement_id and replacement_id not in placements:
            _append_issue(
                validation,
                "unknown_removal_replacement",
                f"Removal {removal_id} references unknown replacement placement {replacement_id}.",
                removal_id=removal_id,
                placement_id=replacement_id,
            )
        valid_removals.append(removal)

    if "get_nearby_entities" in available_tool_names:
        for removal in valid_removals:
            x = float(removal["x"])
            y = float(removal["y"])
            entity_name = str(removal["entity_name"])
            payload = await _nearby(call_mcp, x, y)
            if _matching_live_entity(payload, x, y, entity_name) is not None:
                continue
            replacement_id = str(removal.get("replaced_by", "")).strip()
            replacement = placements.get(replacement_id)
            already_replaced = False
            if replacement is not None:
                rx = _number(replacement.get("x"))
                ry = _number(replacement.get("y"))
                if rx is not None and ry is not None:
                    replacement_payload = await _nearby(call_mcp, rx, ry, radius=2.0)
                    already_replaced = _matching_live_entity(
                        replacement_payload,
                        rx,
                        ry,
                        str(replacement.get("entity_name", "")),
                    ) is not None
            if already_replaced:
                _append_warning(
                    validation,
                    "removal_already_satisfied",
                    f"Removal {removal.get('id')} is already completed and its planned replacement exists.",
                    removal_id=removal.get("id"),
                    placement_id=replacement_id,
                )
            else:
                _append_issue(
                    validation,
                    "removal_target_not_found",
                    f"Planned removal {removal.get('id')} no longer matches a live {entity_name} at ({x},{y}).",
                    removal_id=removal.get("id"),
                )

    routes = [item for item in (plan.get("material_routes") or []) if isinstance(item, dict)]
    for route in routes:
        if str(route.get("source_mode", "")).lower() != "tap":
            continue
        route_id = str(route.get("id", ""))
        source = route.get("source") if isinstance(route.get("source"), dict) else {}
        sx = _number(source.get("x"))
        sy = _number(source.get("y"))
        tap_id = str(route.get("tap_placement_id", "")).strip()
        if not tap_id:
            _append_issue(
                validation,
                "tap_missing_placement",
                f"Tap route {route_id} must declare tap_placement_id for the exact splitter placement.",
                route_id=route_id,
            )
            continue
        placement = placements.get(tap_id)
        if placement is None:
            _append_issue(
                validation,
                "tap_unknown_placement",
                f"Tap route {route_id} references unknown placement {tap_id}.",
                route_id=route_id,
                placement_id=tap_id,
            )
            continue
        if "splitter" not in str(placement.get("entity_name", "")).lower():
            _append_issue(
                validation,
                "tap_not_splitter",
                f"Tap route {route_id} placement {tap_id} must be a splitter.",
                route_id=route_id,
                placement_id=tap_id,
            )
            continue
        if placement.get("allow_replace_existing") is not True:
            _append_issue(
                validation,
                "tap_replacement_not_declared",
                f"Tap splitter {tap_id} must set allow_replace_existing=true and declare the exact source belt removal.",
                route_id=route_id,
                placement_id=tap_id,
            )
        if sx is None or sy is None:
            continue

        lane_pair = _lane_for_source(placement, sx, sy)
        if lane_pair is None:
            _append_issue(
                validation,
                "tap_splitter_misses_source_lane",
                f"Tap splitter {tap_id} does not place either splitter lane on route {route_id} source ({sx},{sy}).",
                route_id=route_id,
                placement_id=tap_id,
            )
            continue

        source_lane, adjacent_lane = lane_pair
        source_removals = [
            removal
            for removal in valid_removals
            if _same(_number(removal.get("x")), source_lane[0])
            and _same(_number(removal.get("y")), source_lane[1])
            and str(removal.get("replaced_by", "")) == tap_id
            and "belt" in str(removal.get("entity_name", "")).lower()
        ]
        if not source_removals:
            _append_issue(
                validation,
                "tap_source_removal_missing",
                f"Tap route {route_id} must declare the exact source belt tile as a removal replaced_by={tap_id}.",
                route_id=route_id,
                placement_id=tap_id,
                source={"x": sx, "y": sy},
            )

        if "get_nearby_entities" in available_tool_names:
            px = _number(placement.get("x"))
            py = _number(placement.get("y"))
            existing_splitter = None
            if px is not None and py is not None:
                placement_payload = await _nearby(call_mcp, px, py, radius=2.0)
                existing_splitter = _matching_live_entity(
                    placement_payload,
                    px,
                    py,
                    str(placement.get("entity_name", "")),
                )
            if existing_splitter is not None:
                _append_warning(
                    validation,
                    "tap_already_satisfied",
                    f"Tap splitter {tap_id} already exists and is treated as completed work.",
                    route_id=route_id,
                    placement_id=tap_id,
                )
                continue

            source_payload = await _nearby(call_mcp, sx, sy)
            source_entity = _matching_live_entity(source_payload, sx, sy)
            if source_entity is None or "belt" not in str(source_entity.get("name", "")).lower():
                _append_issue(
                    validation,
                    "tap_source_belt_not_found",
                    f"Tap route {route_id} has no live belt at source ({sx},{sy}).",
                    route_id=route_id,
                )
            else:
                existing_direction = str(source_entity.get("direction", "")).lower()
                planned_direction = str(placement.get("direction", "")).lower()
                if existing_direction and planned_direction and existing_direction != planned_direction:
                    _append_issue(
                        validation,
                        "tap_splitter_direction_mismatch",
                        f"Tap splitter {tap_id} direction {planned_direction} does not preserve source belt direction {existing_direction}.",
                        route_id=route_id,
                        placement_id=tap_id,
                    )

            ax, ay = adjacent_lane
            adjacent_payload = await _nearby(call_mcp, ax, ay)
            adjacent_entity = _matching_live_entity(adjacent_payload, ax, ay)
            if adjacent_entity is not None and "belt" in str(adjacent_entity.get("name", "")).lower():
                _append_issue(
                    validation,
                    "tap_would_overwrite_adjacent_belt",
                    (
                        f"Tap splitter {tap_id} would span the source belt and a second existing belt at "
                        f"({ax},{ay}). Choose the free side or another tap coordinate; never consume a neighbouring bus lane."
                    ),
                    route_id=route_id,
                    placement_id=tap_id,
                    adjacent_belt={"x": ax, "y": ay, "name": adjacent_entity.get("name")},
                )

    return _finish_validation(validation)


def _point_on_segment(x: float, y: float, segment: dict[str, Any], tolerance: float = 0.11) -> bool:
    start = segment.get("from") if isinstance(segment.get("from"), dict) else {}
    end = segment.get("to") if isinstance(segment.get("to"), dict) else {}
    x1 = _number(start.get("x"))
    y1 = _number(start.get("y"))
    x2 = _number(end.get("x"))
    y2 = _number(end.get("y"))
    if None in {x1, y1, x2, y2}:
        return False
    assert x1 is not None and y1 is not None and x2 is not None and y2 is not None
    if abs(x1 - x2) <= tolerance:
        return abs(x - x1) <= tolerance and min(y1, y2) - tolerance <= y <= max(y1, y2) + tolerance
    if abs(y1 - y2) <= tolerance:
        return abs(y - y1) <= tolerance and min(x1, x2) - tolerance <= x <= max(x1, x2) + tolerance
    return False


def _matches_planned_placement(plan: dict[str, Any], arguments: dict[str, Any]) -> bool:
    entity_name = str(arguments.get("entityName") or arguments.get("entity_name") or "")
    x = _number(arguments.get("x"))
    y = _number(arguments.get("y"))
    direction = str(arguments.get("direction", "")).lower()
    if not entity_name or x is None or y is None:
        return False
    for placement in _plan_placements(plan):
        if str(placement.get("entity_name", "")) != entity_name:
            continue
        if not _same(_number(placement.get("x")), x) or not _same(_number(placement.get("y")), y):
            continue
        planned_direction = str(placement.get("direction", "")).lower()
        if direction and planned_direction and direction != planned_direction:
            continue
        return True
    return False


def _matches_route_belt(plan: dict[str, Any], arguments: dict[str, Any]) -> bool:
    entity_name = str(arguments.get("entityName") or arguments.get("entity_name") or "")
    x = _number(arguments.get("x"))
    y = _number(arguments.get("y"))
    direction = str(arguments.get("direction", "")).lower()
    if x is None or y is None or not entity_name:
        return False
    for route in plan.get("material_routes", []) or []:
        if not isinstance(route, dict) or str(route.get("belt", "")) != entity_name:
            continue
        for segment in route.get("segments", []) or []:
            if not isinstance(segment, dict) or not _point_on_segment(x, y, segment):
                continue
            planned_direction = str(segment.get("direction", "")).lower()
            if direction and planned_direction and direction != planned_direction:
                continue
            return True
    return False


def _matches_planned_removal(plan: dict[str, Any], arguments: dict[str, Any]) -> bool:
    x = _number(arguments.get("x"))
    y = _number(arguments.get("y"))
    if x is None or y is None:
        return False
    entity_name = str(arguments.get("entityName") or arguments.get("entity_name") or "")
    for removal in _plan_removals(plan):
        if not _same(_number(removal.get("x")), x) or not _same(_number(removal.get("y")), y):
            continue
        if entity_name and str(removal.get("entity_name", "")) != entity_name:
            continue
        return True
    return False


def _matches_planned_recipe_change(plan: dict[str, Any], arguments: dict[str, Any]) -> bool:
    x = _number(arguments.get("x"))
    y = _number(arguments.get("y"))
    recipe = str(arguments.get("recipe") or arguments.get("recipeName") or "")
    if x is None or y is None or not recipe:
        return False
    blocks = _production_by_id(plan)
    for placement in _plan_placements(plan):
        if not _same(_number(placement.get("x")), x) or not _same(_number(placement.get("y")), y):
            continue
        block = blocks.get(str(placement.get("block_id", "")))
        if block is not None and str(block.get("recipe", "")) == recipe:
            return True
    return False


def _mutation_authorized(
    name: str,
    arguments: dict[str, Any],
    plan: dict[str, Any],
) -> tuple[bool, str]:
    if not base._is_mutating_tool(name):
        return True, ""
    if name in {"craft", "ensure_item"}:
        return True, ""

    # Batch world mutations are not atomic in FactorioMCP. A call can remove several
    # entities and then fail on a later target, so exact-plan execution uses one mutation
    # per target and checkpoints it independently.
    if name.endswith("_multiple"):
        return False, "batch world mutations are forbidden after PLAN_VALID; execute one planned target at a time"

    if name == "place_entity":
        if _matches_planned_placement(plan, arguments):
            return True, ""
        if _matches_route_belt(plan, arguments):
            return True, ""
        return False, "placement is not an exact validated placement or belt tile on a validated material route"

    if name == "mine_entity":
        if _matches_planned_removal(plan, arguments):
            return True, ""
        return False, "entity removal is not listed in plan.removals"

    if name.startswith(("remove_", "insert_", "transfer_", "pickup_", "drop_", "refuel_")):
        return False, "manual inventory logistics are not part of the validated automated factory plan"

    if name.startswith("set_") and (
        "recipe" in name.lower() or "recipe" in arguments or "recipeName" in arguments
    ):
        if _matches_planned_recipe_change(plan, arguments):
            return True, ""
        return False, "recipe change does not match a validated production-block placement"

    return False, "mutation type is not explicitly represented by the validated plan"


async def _execute_function_calls_plan_conformant(
    session: Any,
    response: Any,
    settings: base.Settings,
    metrics: base.RunMetrics,
    tool_names: set[str],
    plan_gated: bool,
) -> list[dict[str, Any]]:
    if not plan_gated or not metrics.plan_validated or not isinstance(metrics.validated_plan, dict):
        return await _PRIOR_EXECUTE_FUNCTION_CALLS(
            session,
            response,
            settings,
            metrics,
            tool_names,
            plan_gated,
        )

    outputs: list[dict[str, Any]] = []
    for item in getattr(response, "output", []) or []:
        if getattr(item, "type", None) != "function_call":
            continue
        name = str(getattr(item, "name", ""))
        arguments = _arguments(item)
        allowed, reason = _mutation_authorized(name, arguments, metrics.validated_plan)
        if base._is_mutating_tool(name) and not allowed:
            metrics.tool_calls += 1
            metrics.blocked_mutations += 1
            tool_output = (
                "PLAN_EXECUTION_BLOCKED: "
                + reason
                + ". The world was not changed. Do not improvise around this guard. "
                "Revise/resubmit the structured plan if the missing mutation is actually required."
            )
            print(f"[plan-exec] blocked {name}: {reason}", file=sys.stderr)
            outputs.append(
                {
                    "type": "function_call_output",
                    "call_id": item.call_id,
                    "output": tool_output,
                }
            )
            continue

        single_response = SimpleNamespace(output=[item])
        outputs.extend(
            await _PRIOR_EXECUTE_FUNCTION_CALLS(
                session,
                single_response,
                settings,
                metrics,
                tool_names,
                plan_gated,
            )
        )
    return outputs


base.validate_factory_plan = _validate_factory_plan_conformant
resilient.validate_factory_plan = _validate_factory_plan_conformant
base._execute_function_calls = _execute_function_calls_plan_conformant

if _PROMPT_MARKER not in base.SYSTEM_PROMPT:
    base.SYSTEM_PROMPT += """

VALIDATED EXECUTION CONFORMANCE POLICY:
A PLAN_VALID is an executable contract, not permission to improvise. Every source_mode=tap material route must declare tap_placement_id referencing the exact planned splitter. The source belt tile that the splitter replaces must be declared in plan.removals with replaced_by set to that splitter placement. A splitter is two tiles wide across its travel direction: choose a tap position whose second lane is free. Never span, remove, merge, or overwrite a neighbouring main-bus/trunk belt merely to create a tap.

After PLAN_VALID, place machines/splitters/poles/inserters only at exact validated placement coordinates. Ordinary belt tiles may be placed only on validated material-route segments. Mine only one exact entity at a time and only when it appears in plan.removals; batch mining is intentionally blocked because partial batch failure can mutate several targets before reporting failure. Manual remove/insert/transfer/refuel operations are not a substitute for planned automated logistics. If execution needs a mutation that the guard blocks, resubmit a corrected plan rather than redesigning the factory during execution.
"""
