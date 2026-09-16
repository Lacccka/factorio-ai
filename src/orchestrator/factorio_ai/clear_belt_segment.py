from __future__ import annotations

import json
import math
import sys
from types import SimpleNamespace
from typing import Any

from . import app as base
from . import architectural_execution_guard as architectural
from . import persistent_app as persistent


TOOL_NAME = "build_clear_belt_segment"
MAX_SEGMENT_TILES = 64
_PROMPT_MARKER = "CLEAR BELT SEGMENT EXECUTION"
_ALLOWED_BELTS = {"transport-belt", "fast-transport-belt", "express-transport-belt"}

TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "name": TOOL_NAME,
    "description": (
        "Build one straight surface-belt run selected by you. The runtime first preflights every tile; "
        "if any tile is occupied, nothing is built and the exact blocked coordinates are returned. "
        "Use this for already-understood clear corridors after PLAN_VALID, not for crossing dense factory geometry."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "entityName": {
                "type": "string",
                "enum": ["transport-belt", "fast-transport-belt", "express-transport-belt"],
            },
            "startX": {"type": "number"},
            "startY": {"type": "number"},
            "endX": {"type": "number"},
            "endY": {"type": "number"},
        },
        "required": ["entityName", "startX", "startY", "endX", "endY"],
    },
}

_PRIOR_ACTIVE_TOOLS = base._active_tools
_PRIOR_EXECUTE_FUNCTION_CALLS = base._execute_function_calls
base.MUTATING_TOOL_NAMES.add(TOOL_NAME)


def _parse_json(text: str) -> dict[str, Any] | None:
    clean = text.split("\n\nMUTATION_BUDGET_REACHED:", 1)[0].strip()
    if clean.startswith(("MCP_TOOL_ERROR:", "MCP_TOOL_EXCEPTION:", "INVALID_TOOL_ARGUMENTS:")):
        return None
    try:
        value = json.loads(clean)
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _tile_center(value: float) -> bool:
    # Vanilla belt entity centers lie on N+0.5 coordinates.
    return abs((value - 0.5) - round(value - 0.5)) <= 0.05


def _segment_placements(arguments: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None]:
    entity_name = str(arguments.get("entityName") or "")
    if entity_name not in _ALLOWED_BELTS:
        return [], f"entityName must be one of {sorted(_ALLOWED_BELTS)}"

    sx = _number(arguments.get("startX"))
    sy = _number(arguments.get("startY"))
    ex = _number(arguments.get("endX"))
    ey = _number(arguments.get("endY"))
    if None in {sx, sy, ex, ey}:
        return [], "start/end coordinates must be numeric"
    assert sx is not None and sy is not None and ex is not None and ey is not None

    if not all(_tile_center(value) for value in (sx, sy, ex, ey)):
        return [], "belt coordinates must use tile centers ending in .5"

    dx = ex - sx
    dy = ey - sy
    if abs(dx) <= 0.05 and abs(dy) <= 0.05:
        return [], "segment must contain at least two distinct belt tiles"
    if abs(dx) > 0.05 and abs(dy) > 0.05:
        return [], "segment must be axis-aligned; choose turns explicitly"

    distance = abs(dx) if abs(dx) > 0.05 else abs(dy)
    steps = int(round(distance))
    if abs(distance - steps) > 0.05:
        return [], "segment endpoints must be an integer number of tiles apart"
    tile_count = steps + 1
    if tile_count > MAX_SEGMENT_TILES:
        return [], f"segment contains {tile_count} tiles; maximum is {MAX_SEGMENT_TILES}"

    if abs(dx) > 0.05:
        direction = "east" if dx > 0 else "west"
        vx, vy = (1.0 if dx > 0 else -1.0), 0.0
    else:
        direction = "south" if dy > 0 else "north"
        vx, vy = 0.0, (1.0 if dy > 0 else -1.0)

    placements: list[dict[str, Any]] = []
    for index in range(tile_count):
        placements.append(
            {
                "id": f"segment-{index}",
                "entity_name": entity_name,
                "entityName": entity_name,
                "x": round(sx + vx * index, 3),
                "y": round(sy + vy * index, 3),
                "direction": direction,
            }
        )
    return placements, None


def _active_tools_with_clear_segment(
    all_tools: list[dict[str, Any]],
    gated_tools: list[dict[str, Any]],
    read_only_tools: list[dict[str, Any]],
    metrics: base.RunMetrics,
    plan_gated: bool,
) -> list[dict[str, Any]]:
    tools = _PRIOR_ACTIVE_TOOLS(all_tools, gated_tools, read_only_tools, metrics, plan_gated)
    if (
        plan_gated
        and metrics.plan_validated
        and not metrics.mutation_budget_exhausted
        and not any(str(tool.get("name", "")) == TOOL_NAME for tool in tools)
    ):
        return [*tools, TOOL_SCHEMA]
    return tools


async def _call_local(
    session: Any,
    name: str,
    arguments: dict[str, Any],
    settings: base.Settings,
    metrics: base.RunMetrics,
) -> str:
    metrics.tool_calls += 1
    return await base._call_tool(session, name, arguments, settings.tool_result_max_chars)


async def _describe_blocker(
    session: Any,
    result: dict[str, Any],
    settings: base.Settings,
    metrics: base.RunMetrics,
) -> dict[str, Any]:
    x = _number(result.get("x"))
    y = _number(result.get("y"))
    detail = {
        "x": x,
        "y": y,
        "entity_name": result.get("entity_name"),
        "direction": result.get("direction"),
    }
    if x is None or y is None:
        return detail
    output = await _call_local(
        session,
        "get_nearby_entities",
        {"radius": 1.25, "centerX": x, "centerY": y},
        settings,
        metrics,
    )
    payload = _parse_json(output)
    if isinstance(payload, dict):
        exact = []
        for entity in payload.get("entities", []) or []:
            if not isinstance(entity, dict):
                continue
            ex = _number(entity.get("x"))
            ey = _number(entity.get("y"))
            if ex is not None and ey is not None and abs(ex - x) <= 0.65 and abs(ey - y) <= 0.65:
                exact.append(
                    {
                        "name": entity.get("name"),
                        "type": entity.get("type"),
                        "x": ex,
                        "y": ey,
                        "direction": entity.get("direction"),
                    }
                )
        if exact:
            detail["existing"] = exact[:4]
    return detail


def _record_success(metrics: base.RunMetrics, arguments: dict[str, Any], output: str) -> None:
    store = persistent._RUN_STORES.get(id(metrics))
    if store is None:
        return
    store.mark_world_mutation("place_entity", arguments)
    store.record_plan_execution_step("place_entity", arguments, output, False)


async def _place_one_with_range_recovery(
    session: Any,
    arguments: dict[str, Any],
    settings: base.Settings,
    metrics: base.RunMetrics,
) -> tuple[str, bool]:
    if base._budget_exhausted(settings, metrics):
        metrics.mutation_budget_exhausted = True
        return base._budget_notice(settings, metrics), False

    metrics.tool_calls += 1
    metrics.mutation_calls += 1
    output = await base._call_tool(session, "place_entity", arguments, settings.tool_result_max_chars)
    payload = _parse_json(output)
    if isinstance(payload, dict) and str(payload.get("error", "")).lower() == "out_of_range":
        metrics.mutation_calls = max(0, metrics.mutation_calls - 1)
        walk = await _call_local(
            session,
            "walk_to_position",
            {
                "targetX": arguments["x"],
                "targetY": arguments["y"],
                "tolerance": 5,
                "timeoutSeconds": 30,
            },
            settings,
            metrics,
        )
        walk_payload = _parse_json(walk)
        if not isinstance(walk_payload, dict) or str(walk_payload.get("status", "")).lower() != "arrived":
            return json.dumps(
                {
                    "success": False,
                    "error": "range_recovery_failed",
                    "movement": walk[:500],
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ), False
        metrics.tool_calls += 1
        metrics.mutation_calls += 1
        output = await base._call_tool(session, "place_entity", arguments, settings.tool_result_max_chars)

    failed = base._is_tool_failure(output)
    if failed:
        metrics.failed_mutations += 1
    else:
        _record_success(metrics, arguments, output)

    if base._budget_exhausted(settings, metrics):
        metrics.mutation_budget_exhausted = True
    return output, not failed


async def _build_clear_segment(
    session: Any,
    arguments: dict[str, Any],
    settings: base.Settings,
    metrics: base.RunMetrics,
    tool_names: set[str],
) -> str:
    placements, error = _segment_placements(arguments)
    if error:
        return json.dumps({"success": False, "status": "invalid_request", "error": error}, ensure_ascii=False)
    if not isinstance(metrics.validated_plan, dict):
        return json.dumps(
            {"success": False, "status": "blocked", "error": "PLAN_VALID architecture is required"},
            ensure_ascii=False,
        )
    if "check_entity_placement_batch" not in tool_names:
        return json.dumps(
            {"success": False, "status": "unavailable", "error": "check_entity_placement_batch is unavailable"},
            ensure_ascii=False,
        )

    for placement in placements:
        args = {
            "entityName": placement["entityName"],
            "x": placement["x"],
            "y": placement["y"],
            "direction": placement["direction"],
        }
        allowed, reason = architectural._architectural_mutation_authorized(
            "place_entity",
            args,
            metrics.validated_plan,
        )
        if not allowed:
            return json.dumps(
                {
                    "success": False,
                    "status": "policy_blocked",
                    "blocked_at": {"x": placement["x"], "y": placement["y"]},
                    "reason": reason,
                    "no_world_change": True,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )

    preflight = [
        {
            "id": placement["id"],
            "entity_name": placement["entity_name"],
            "x": placement["x"],
            "y": placement["y"],
            "direction": placement["direction"],
        }
        for placement in placements
    ]
    preflight_output = await _call_local(
        session,
        "check_entity_placement_batch",
        {"placementsJson": json.dumps(preflight, separators=(",", ":"))},
        settings,
        metrics,
    )
    payload = _parse_json(preflight_output)
    if not isinstance(payload, dict) or payload.get("success") is False:
        return json.dumps(
            {
                "success": False,
                "status": "preflight_failed",
                "error": preflight_output[:1000],
                "no_world_change": True,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    blocked = [
        result
        for result in (payload.get("results") or [])
        if isinstance(result, dict) and result.get("can_place") is False
    ]
    if blocked:
        details: list[dict[str, Any]] = []
        for result in blocked[:4]:
            details.append(await _describe_blocker(session, result, settings, metrics))
        return json.dumps(
            {
                "success": False,
                "status": "blocked",
                "blocked_count": len(blocked),
                "blocked": details,
                "no_world_change": True,
                "message": (
                    "The full straight run was preflighted and no tiles were placed. Inspect/adapt only around the reported blocker; "
                    "use underground belts or choose a different local path, then call this tool again for the next clear run."
                ),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    remaining = settings.max_mutations - metrics.mutation_calls
    if remaining < len(placements):
        return json.dumps(
            {
                "success": False,
                "status": "budget_insufficient",
                "needed_mutations": len(placements),
                "remaining_mutations": max(0, remaining),
                "no_world_change": True,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    completed: list[dict[str, Any]] = []
    for placement in placements:
        args = {
            "entityName": placement["entityName"],
            "x": placement["x"],
            "y": placement["y"],
            "direction": placement["direction"],
        }
        output, ok = await _place_one_with_range_recovery(session, args, settings, metrics)
        if not ok:
            return json.dumps(
                {
                    "success": False,
                    "status": "partial_runtime_failure",
                    "placed_count": len(completed),
                    "placed_through": completed[-1] if completed else None,
                    "failed_at": {"x": placement["x"], "y": placement["y"]},
                    "tool_output": output[:1000],
                    "message": (
                        "Preflight was clear but the live world changed or execution failed while placing. Stop this run here, inspect the exact failed tile, "
                        "and adapt before issuing more dependent construction."
                    ),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        completed.append({"x": placement["x"], "y": placement["y"]})

    first = placements[0]
    last = placements[-1]
    return json.dumps(
        {
            "success": True,
            "status": "built",
            "entity": first["entityName"],
            "direction": first["direction"],
            "placed_count": len(completed),
            "start": {"x": first["x"], "y": first["y"]},
            "end": {"x": last["x"], "y": last["y"]},
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _arguments(item: Any) -> tuple[dict[str, Any] | None, str | None]:
    try:
        value = json.loads(getattr(item, "arguments", "") or "{}")
    except Exception as exc:
        return None, str(exc)
    if not isinstance(value, dict):
        return None, "function arguments must decode to an object"
    return value, None


async def _execute_function_calls_with_clear_segment(
    session: Any,
    response: Any,
    settings: base.Settings,
    metrics: base.RunMetrics,
    tool_names: set[str],
    plan_gated: bool,
) -> list[dict[str, Any]]:
    calls = [
        item
        for item in getattr(response, "output", []) or []
        if getattr(item, "type", None) == "function_call"
    ]
    if not any(str(getattr(item, "name", "")) == TOOL_NAME for item in calls):
        return await _PRIOR_EXECUTE_FUNCTION_CALLS(
            session,
            response,
            settings,
            metrics,
            tool_names,
            plan_gated,
        )

    outputs: list[dict[str, Any]] = []
    stop_after_failure = False
    for item in calls:
        name = str(getattr(item, "name", ""))
        if name != TOOL_NAME:
            if stop_after_failure and base._is_mutating_tool(name):
                metrics.tool_calls += 1
                metrics.blocked_mutations += 1
                outputs.append(
                    {
                        "type": "function_call_output",
                        "call_id": item.call_id,
                        "output": "EXECUTION_DEFERRED: a preceding clear-segment action failed; observe/adapt before more dependent mutations.",
                    }
                )
                continue
            delegated = await _PRIOR_EXECUTE_FUNCTION_CALLS(
                session,
                SimpleNamespace(output=[item]),
                settings,
                metrics,
                tool_names,
                plan_gated,
            )
            outputs.extend(delegated)
            if base._is_mutating_tool(name) and any(
                base._is_tool_failure(str(output.get("output", ""))) for output in delegated
            ):
                stop_after_failure = True
            continue

        metrics.tool_calls += 1
        if not plan_gated or not metrics.plan_validated:
            tool_output = "CLEAR_SEGMENT_BLOCKED: build_clear_belt_segment is available only after current-process PLAN_VALID."
            metrics.blocked_mutations += 1
            stop_after_failure = True
        elif metrics.mutation_budget_exhausted:
            tool_output = base._budget_notice(settings, metrics)
            metrics.blocked_mutations += 1
            stop_after_failure = True
        else:
            arguments, parse_error = _arguments(item)
            if parse_error or arguments is None:
                tool_output = f"INVALID_TOOL_ARGUMENTS: {parse_error}"
                metrics.failed_mutations += 1
                stop_after_failure = True
            else:
                tool_output = await _build_clear_segment(
                    session,
                    arguments,
                    settings,
                    metrics,
                    tool_names,
                )
                payload = _parse_json(tool_output)
                if not isinstance(payload, dict) or payload.get("success") is not True:
                    stop_after_failure = True
        print(f"[clear-segment] {tool_output[:500]}", file=sys.stderr)
        outputs.append(
            {
                "type": "function_call_output",
                "call_id": item.call_id,
                "output": tool_output,
            }
        )
    return outputs


base._active_tools = _active_tools_with_clear_segment
base._execute_function_calls = _execute_function_calls_with_clear_segment

if _PROMPT_MARKER not in base.SYSTEM_PROMPT:
    base.SYSTEM_PROMPT += """

CLEAR BELT SEGMENT EXECUTION:
Do not spend cloud reasoning turns placing a long known-clear straight belt one tile at a time. After PLAN_VALID, once you have inspected enough local topology to know a straight corridor is appropriate, use build_clear_belt_segment for that straight run. It preflights the entire requested run before mutation. If any tile is occupied it builds nothing and reports the exact blocker, so you can reason locally about an underground crossing or detour. Use ordinary place_entity for corners, underground endpoints and short local corrections. Keep clear-segment calls reasonably local; never use them as a substitute for understanding a dense existing factory area.
"""
