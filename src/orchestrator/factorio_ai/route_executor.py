from __future__ import annotations

import json
import math
import sys
from types import SimpleNamespace
from typing import Any

from . import app as base
from . import persistent_app as persistent
from . import plan_execution_guard as guard


BUILD_ROUTE_TOOL_NAME = "build_route_segment"
_MAX_TILES_PER_CALL = 64
_PROMPT_MARKER = "DETERMINISTIC LOCAL ROUTE EXECUTION"

BUILD_ROUTE_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "name": BUILD_ROUTE_TOOL_NAME,
    "description": (
        "Build one straight surface-belt segment deterministically after PLAN_VALID. The orchestrator handles range/walking, "
        "idempotent existing belts, and stops at the first live obstacle with compact blocker details. Use several short "
        "segments to make a local detour. This tool cannot mine entities, create splitter taps, or leave the authorized route corridor."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "entity_name": {
                "type": "string",
                "enum": ["transport-belt", "fast-transport-belt", "express-transport-belt"],
            },
            "from": {
                "type": "object",
                "properties": {"x": {"type": "number"}, "y": {"type": "number"}},
                "required": ["x", "y"],
            },
            "to": {
                "type": "object",
                "properties": {"x": {"type": "number"}, "y": {"type": "number"}},
                "required": ["x", "y"],
            },
            "direction": {"type": "string", "enum": ["north", "south", "east", "west"]},
        },
        "required": ["entity_name", "from", "to", "direction"],
    },
}

_PRIOR_ACTIVE_TOOLS = base._active_tools
_PRIOR_EXECUTE_FUNCTION_CALLS = base._execute_function_calls


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_json(output: str) -> dict[str, Any] | None:
    text = output.split("\n\nMUTATION_BUDGET_REACHED:", 1)[0].strip()
    try:
        value = json.loads(text)
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def _arguments(item: Any) -> dict[str, Any]:
    try:
        value = json.loads(getattr(item, "arguments", "") or "{}")
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _segment_targets(arguments: dict[str, Any]) -> tuple[list[dict[str, Any]] | None, str | None]:
    entity_name = str(arguments.get("entity_name", "")).strip()
    start = arguments.get("from") if isinstance(arguments.get("from"), dict) else {}
    end = arguments.get("to") if isinstance(arguments.get("to"), dict) else {}
    x1 = _number(start.get("x"))
    y1 = _number(start.get("y"))
    x2 = _number(end.get("x"))
    y2 = _number(end.get("y"))
    direction = str(arguments.get("direction", "")).lower()
    if entity_name not in {"transport-belt", "fast-transport-belt", "express-transport-belt"}:
        return None, "unsupported belt entity"
    if None in {x1, y1, x2, y2}:
        return None, "from/to must contain numeric x,y"
    assert x1 is not None and y1 is not None and x2 is not None and y2 is not None
    dx = x2 - x1
    dy = y2 - y1
    if abs(dx) > 0.05 and abs(dy) > 0.05:
        return None, "segment must be axis-aligned"
    distance = abs(dx) + abs(dy)
    steps = int(round(distance))
    if abs(distance - steps) > 0.05:
        return None, "segment endpoints must lie on the same one-tile grid"
    if steps + 1 > _MAX_TILES_PER_CALL:
        return None, f"segment contains {steps + 1} tiles; maximum is {_MAX_TILES_PER_CALL}"

    expected_vector = {
        "north": (0.0, -1.0),
        "south": (0.0, 1.0),
        "east": (1.0, 0.0),
        "west": (-1.0, 0.0),
    }.get(direction)
    if expected_vector is None:
        return None, "unsupported direction"
    if steps:
        actual = (
            0.0 if abs(dx) <= 0.05 else math.copysign(1.0, dx),
            0.0 if abs(dy) <= 0.05 else math.copysign(1.0, dy),
        )
        if actual != expected_vector:
            return None, f"coordinate travel {actual} does not match belt direction {direction}"
    else:
        actual = expected_vector

    return [
        {
            "entityName": entity_name,
            "x": x1 + actual[0] * index,
            "y": y1 + actual[1] * index,
            "direction": direction,
        }
        for index in range(steps + 1)
    ], None


def _same_existing_belt(payload: dict[str, Any] | None, target: dict[str, Any]) -> bool:
    if not isinstance(payload, dict) or payload.get("success") is not True:
        return False
    name = str(payload.get("entity", ""))
    direction = str(payload.get("direction", "")).lower()
    return name == str(target["entityName"]) and (not direction or direction == str(target["direction"]).lower())


def _compact_existing(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    return {
        "entity": payload.get("entity"),
        "type": payload.get("type"),
        "position": payload.get("position"),
        "direction": payload.get("direction"),
        "status": payload.get("status"),
        "error": payload.get("error"),
    }


def _active_tools_with_route_executor(
    all_tools: list[dict[str, Any]],
    gated_tools: list[dict[str, Any]],
    read_only_tools: list[dict[str, Any]],
    metrics: base.RunMetrics,
    plan_gated: bool,
) -> list[dict[str, Any]]:
    tools = _PRIOR_ACTIVE_TOOLS(
        all_tools,
        gated_tools,
        read_only_tools,
        metrics,
        plan_gated,
    )
    if not plan_gated or not metrics.plan_validated or metrics.mutation_budget_exhausted:
        return tools
    names = {str(tool.get("name", "")) for tool in tools}
    if {"place_entity", "walk_to_position", "inspect_entity"}.issubset(names):
        return [*tools, BUILD_ROUTE_TOOL_SCHEMA]
    return tools


def _record_success(metrics: base.RunMetrics, target: dict[str, Any], output: str) -> None:
    store = persistent._RUN_STORES.get(id(metrics))
    if store is None:
        return
    store.mark_world_mutation("place_entity", target)
    store.record_plan_execution_step("place_entity", target, output, False)


async def _place_target(
    session: Any,
    target: dict[str, Any],
    settings: base.Settings,
    metrics: base.RunMetrics,
) -> tuple[str, dict[str, Any] | None]:
    if base._budget_exhausted(settings, metrics):
        metrics.mutation_budget_exhausted = True
        return "budget", None

    metrics.mutation_calls += 1
    output = await base._call_tool(session, "place_entity", target, settings.tool_result_max_chars)
    payload = _parse_json(output)
    if isinstance(payload, dict) and payload.get("success") is True:
        _record_success(metrics, target, output)
        return "placed", payload

    error = str((payload or {}).get("error", "")).lower()
    if error == "out_of_range":
        walk_output = await base._call_tool(
            session,
            "walk_to_position",
            {
                "targetX": target["x"],
                "targetY": target["y"],
                "tolerance": 5,
                "timeoutSeconds": 30,
            },
            settings.tool_result_max_chars,
        )
        walk_payload = _parse_json(walk_output)
        if not isinstance(walk_payload, dict) or str(walk_payload.get("status", "")).lower() != "arrived":
            return "movement_blocked", walk_payload
        if base._budget_exhausted(settings, metrics):
            metrics.mutation_budget_exhausted = True
            return "budget", None
        metrics.mutation_calls += 1
        output = await base._call_tool(session, "place_entity", target, settings.tool_result_max_chars)
        payload = _parse_json(output)
        if isinstance(payload, dict) and payload.get("success") is True:
            _record_success(metrics, target, output)
            return "placed", payload
        error = str((payload or {}).get("error", "")).lower()

    if error in {"invalid_position", "cannot_place", "collision", "blocked"}:
        inspect_output = await base._call_tool(
            session,
            "inspect_entity",
            {"x": target["x"], "y": target["y"]},
            settings.tool_result_max_chars,
        )
        existing = _parse_json(inspect_output)
        if _same_existing_belt(existing, target):
            return "already_present", existing
        return "blocked", existing

    metrics.failed_mutations += 1
    if base._budget_exhausted(settings, metrics):
        metrics.mutation_budget_exhausted = True
    return "failed", payload


async def _build_route_segment(
    session: Any,
    arguments: dict[str, Any],
    settings: base.Settings,
    metrics: base.RunMetrics,
    plan: dict[str, Any],
) -> dict[str, Any]:
    targets, error = _segment_targets(arguments)
    if targets is None:
        return {"success": False, "status": "invalid_arguments", "error": error}

    # Authorize the complete segment before mutating anything. This is a corridor/safety
    # check, not a collision preflight: live obstacles are intentionally discovered during
    # incremental execution so Sol can adapt locally.
    for index, target in enumerate(targets):
        allowed, reason = guard._mutation_authorized("place_entity", target, plan)
        if not allowed:
            return {
                "success": False,
                "status": "blocked_by_policy",
                "index": index,
                "at": {"x": target["x"], "y": target["y"]},
                "reason": reason,
            }

    placed = 0
    already_present = 0
    last_completed: dict[str, Any] | None = None
    for index, target in enumerate(targets):
        status, payload = await _place_target(session, target, settings, metrics)
        if status == "placed":
            placed += 1
            last_completed = {"x": target["x"], "y": target["y"]}
            continue
        if status == "already_present":
            already_present += 1
            last_completed = {"x": target["x"], "y": target["y"]}
            continue
        if status == "budget":
            return {
                "success": False,
                "status": "mutation_budget_reached",
                "placed": placed,
                "already_present": already_present,
                "last_completed": last_completed,
                "at": {"x": target["x"], "y": target["y"]},
            }
        if status == "movement_blocked":
            return {
                "success": False,
                "status": "movement_blocked",
                "placed": placed,
                "already_present": already_present,
                "last_completed": last_completed,
                "at": {"x": target["x"], "y": target["y"]},
                "movement": payload,
            }
        if status == "blocked":
            return {
                "success": False,
                "status": "blocked",
                "placed": placed,
                "already_present": already_present,
                "last_completed": last_completed,
                "blocked_at": {"x": target["x"], "y": target["y"]},
                "existing": _compact_existing(payload),
                "guidance": "Inspect only this local obstacle and choose a short detour inside the same route corridor; do not resubmit the global plan.",
            }
        return {
            "success": False,
            "status": "tool_failure",
            "placed": placed,
            "already_present": already_present,
            "last_completed": last_completed,
            "at": {"x": target["x"], "y": target["y"]},
            "result": payload,
        }

    return {
        "success": True,
        "status": "complete",
        "placed": placed,
        "already_present": already_present,
        "tile_count": len(targets),
        "last_completed": last_completed,
    }


async def _execute_function_calls_with_route_executor(
    session: Any,
    response: Any,
    settings: base.Settings,
    metrics: base.RunMetrics,
    tool_names: set[str],
    plan_gated: bool,
) -> list[dict[str, Any]]:
    outputs: list[dict[str, Any]] = []
    for item in getattr(response, "output", []) or []:
        if getattr(item, "type", None) != "function_call":
            continue
        if str(getattr(item, "name", "")) != BUILD_ROUTE_TOOL_NAME:
            outputs.extend(
                await _PRIOR_EXECUTE_FUNCTION_CALLS(
                    session,
                    SimpleNamespace(output=[item]),
                    settings,
                    metrics,
                    tool_names,
                    plan_gated,
                )
            )
            continue

        metrics.tool_calls += 1
        if (
            not plan_gated
            or not metrics.plan_validated
            or not isinstance(metrics.validated_plan, dict)
        ):
            result = {
                "success": False,
                "status": "plan_required",
                "error": "build_route_segment is only available after PLAN_VALID",
            }
        elif not {"place_entity", "walk_to_position", "inspect_entity"}.issubset(tool_names):
            result = {
                "success": False,
                "status": "unsupported_runtime",
                "error": "required FactorioMCP placement/movement tools are unavailable",
            }
        else:
            arguments = _arguments(item)
            result = await _build_route_segment(
                session,
                arguments,
                settings,
                metrics,
                metrics.validated_plan,
            )
        tool_output = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        print(f"[route-exec] {tool_output[:500]}", file=sys.stderr)
        outputs.append(
            {
                "type": "function_call_output",
                "call_id": item.call_id,
                "output": tool_output,
            }
        )
    return outputs


base._active_tools = _active_tools_with_route_executor
base._execute_function_calls = _execute_function_calls_with_route_executor

if _PROMPT_MARKER not in base.SYSTEM_PROMPT:
    base.SYSTEM_PROMPT += """

DETERMINISTIC LOCAL ROUTE EXECUTION:
Prefer build_route_segment for straight surface-belt runs after PLAN_VALID instead of issuing one place_entity call per tile. It executes the authorized segment locally without consuming a cloud turn per belt tile, handles walking/range and already-built belts, and stops at the first real obstacle.

When build_route_segment returns status=blocked, use the returned blocked_at/existing facts to reason about a short local detour. Inspect only the immediate area when needed, then call build_route_segment for the detour legs. Do not convert a local blocked tile into a global PLAN_INVALID or repeat broad planning. Splitter taps, destructive removals, underground endpoints, machines and inserters continue to use their normal guarded tools.
"""
