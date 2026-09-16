from __future__ import annotations

import json
import math
from collections import defaultdict, deque
from typing import Any, Awaitable, Callable

from . import app as base
from . import resilient_app as resilient
from .planning import POLE_SPECS


CallMcp = Callable[[str, dict[str, Any]], Awaitable[str]]
_PRIOR_VALIDATE_FACTORY_PLAN = base.validate_factory_plan


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _point(value: Any) -> tuple[float, float] | None:
    if not isinstance(value, dict):
        return None
    x = _number(value.get("x"))
    y = _number(value.get("y"))
    if x is None or y is None:
        return None
    return x, y


def _parse_json(output: str) -> dict[str, Any] | None:
    text = output.split("\n\nMUTATION_BUDGET_REACHED:", 1)[0].strip()
    if text.startswith(("MCP_TOOL_ERROR:", "MCP_TOOL_EXCEPTION:", "INVALID_TOOL_ARGUMENTS:")):
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
    return validation


def _exact_anchor_pole(payload: dict[str, Any] | None, anchor: tuple[float, float]) -> str | None:
    if not isinstance(payload, dict):
        return None
    ax, ay = anchor
    for entity in payload.get("entities", []) or []:
        if not isinstance(entity, dict):
            continue
        x = _number(entity.get("x"))
        y = _number(entity.get("y"))
        name = str(entity.get("name", ""))
        if x is None or y is None or abs(x - ax) > 0.6 or abs(y - ay) > 0.6:
            continue
        if name in POLE_SPECS:
            return name
    return None


def _reachable_planned_poles(
    anchor: tuple[float, float],
    anchor_type: str,
    pole_type: str,
    planned_poles: list[tuple[str, float, float]],
) -> set[str]:
    anchor_spec = POLE_SPECS.get(anchor_type)
    planned_spec = POLE_SPECS.get(pole_type)
    if anchor_spec is None or planned_spec is None:
        return set()

    anchor_reach = float(anchor_spec[1])
    planned_reach = float(planned_spec[1])
    nodes: list[tuple[str, float, float, float]] = [
        ("__anchor__", anchor[0], anchor[1], anchor_reach),
        *[(pole_id, x, y, planned_reach) for pole_id, x, y in planned_poles],
    ]
    adjacency: dict[str, set[str]] = defaultdict(set)
    for index, (a_id, ax, ay, a_reach) in enumerate(nodes):
        for b_id, bx, by, b_reach in nodes[index + 1 :]:
            # Factorio copper-wire auto-connect requires both poles to be within the
            # connection distance. For mixed pole types the shorter reach is decisive.
            if math.hypot(ax - bx, ay - by) <= min(a_reach, b_reach) + 1e-9:
                adjacency[a_id].add(b_id)
                adjacency[b_id].add(a_id)

    seen = {"__anchor__"}
    queue: deque[str] = deque(["__anchor__"])
    while queue:
        current = queue.popleft()
        for neighbour in adjacency[current]:
            if neighbour in seen:
                continue
            seen.add(neighbour)
            queue.append(neighbour)
    return seen


async def _validate_factory_plan_actual_wire_reach(
    plan: Any,
    call_mcp: CallMcp,
    available_tool_names: set[str],
) -> dict[str, Any]:
    validation = await _PRIOR_VALIDATE_FACTORY_PLAN(plan, call_mcp, available_tool_names)
    if not isinstance(validation, dict):
        validation = {"valid": False, "status": "PLAN_INVALID", "issues": [], "warnings": []}
    if not isinstance(plan, dict) or "get_nearby_entities" not in available_tool_names:
        return validation

    power = plan.get("power")
    placements = plan.get("placements")
    if not isinstance(power, dict) or not isinstance(placements, list):
        return validation

    pole_type = str(power.get("pole_type", "")).strip()
    anchor = _point(power.get("existing_anchor"))
    if pole_type not in POLE_SPECS or anchor is None:
        return validation

    placement_map = {
        str(item.get("id", "")): item
        for item in placements
        if isinstance(item, dict) and str(item.get("id", ""))
    }
    planned_poles: list[tuple[str, float, float]] = []
    for pole_id in power.get("pole_ids", []) or []:
        pole_id = str(pole_id)
        placement = placement_map.get(pole_id)
        if not isinstance(placement, dict) or str(placement.get("entity_name", "")) != pole_type:
            continue
        x = _number(placement.get("x"))
        y = _number(placement.get("y"))
        if x is not None and y is not None:
            planned_poles.append((pole_id, x, y))
    if not planned_poles:
        return validation

    nearby = _parse_json(
        await call_mcp(
            "get_nearby_entities",
            {"radius": 1.5, "centerX": anchor[0], "centerY": anchor[1]},
        )
    )
    anchor_type = _exact_anchor_pole(nearby, anchor)
    if anchor_type is None:
        # The inner validator already reports power_anchor_not_found when appropriate.
        return validation

    reachable = _reachable_planned_poles(anchor, anchor_type, pole_type, planned_poles)
    disconnected = [pole_id for pole_id, _, _ in planned_poles if pole_id not in reachable]
    existing_codes = {
        str(issue.get("code", ""))
        for issue in (validation.get("issues") or [])
        if isinstance(issue, dict)
    }
    if disconnected and "power_network_disconnected_actual_wire_reach" not in existing_codes:
        issues = validation.setdefault("issues", [])
        if not isinstance(issues, list):
            issues = []
            validation["issues"] = issues
        issues.append(
            {
                "code": "power_network_disconnected_actual_wire_reach",
                "message": (
                    f"Planned {pole_type} chain is not actually connectable to the existing "
                    f"{anchor_type} anchor using the shorter pole's wire reach: {', '.join(disconnected)}"
                ),
                "placement_ids": disconnected,
                "anchor_type": anchor_type,
                "planned_pole_type": pole_type,
            }
        )
    return _finish(validation)


base.validate_factory_plan = _validate_factory_plan_actual_wire_reach
resilient.validate_factory_plan = _validate_factory_plan_actual_wire_reach
