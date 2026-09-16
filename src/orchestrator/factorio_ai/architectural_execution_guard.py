from __future__ import annotations

import math
from typing import Any

from . import app as base
from . import plan_execution_guard as guard
from .planning import POLE_SPECS


# PLAN_VALID is an architectural contract. Destructive changes, source taps, recipes,
# machines and inserters remain exact. Additive belt/pole geometry may adapt locally to
# the live world so the reasoning model can route around obstacles without turning every
# blocked tile into a full factory re-plan.
_STRICT_MUTATION_AUTHORIZED = guard._mutation_authorized
_PROMPT_MARKER = "ARCHITECTURAL PLAN EXECUTION POLICY"
_LOCAL_ROUTE_RADIUS = 5.0
_LOCAL_POLE_RADIUS = 4.0


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _point_segment_distance(x: float, y: float, segment: dict[str, Any]) -> float | None:
    start = segment.get("from") if isinstance(segment.get("from"), dict) else {}
    end = segment.get("to") if isinstance(segment.get("to"), dict) else {}
    x1 = _number(start.get("x"))
    y1 = _number(start.get("y"))
    x2 = _number(end.get("x"))
    y2 = _number(end.get("y"))
    if None in {x1, y1, x2, y2}:
        return None
    assert x1 is not None and y1 is not None and x2 is not None and y2 is not None
    dx = x2 - x1
    dy = y2 - y1
    length_sq = dx * dx + dy * dy
    if length_sq <= 1e-12:
        return math.hypot(x - x1, y - y1)
    t = ((x - x1) * dx + (y - y1) * dy) / length_sq
    t = max(0.0, min(1.0, t))
    nearest_x = x1 + t * dx
    nearest_y = y1 + t * dy
    return math.hypot(x - nearest_x, y - nearest_y)


def _route_accepts_entity(route: dict[str, Any], entity_name: str) -> bool:
    belt = str(route.get("belt", "")).strip()
    if not belt:
        return False
    if entity_name == belt:
        return True
    underground_for = {
        "transport-belt": "underground-belt",
        "fast-transport-belt": "fast-underground-belt",
        "express-transport-belt": "express-underground-belt",
    }
    return entity_name == underground_for.get(belt)


def _near_material_route(
    plan: dict[str, Any],
    entity_name: str,
    x: float,
    y: float,
    radius: float = _LOCAL_ROUTE_RADIUS,
) -> bool:
    for route in plan.get("material_routes", []) or []:
        if not isinstance(route, dict) or not _route_accepts_entity(route, entity_name):
            continue
        for segment in route.get("segments", []) or []:
            if not isinstance(segment, dict):
                continue
            distance = _point_segment_distance(x, y, segment)
            if distance is not None and distance <= radius + 1e-9:
                return True
    return False


def _near_planned_pole(plan: dict[str, Any], entity_name: str, x: float, y: float) -> bool:
    power = plan.get("power") if isinstance(plan.get("power"), dict) else {}
    planned_type = str(power.get("pole_type", "")).strip()
    if entity_name not in POLE_SPECS or entity_name != planned_type:
        return False
    pole_ids = {str(value) for value in power.get("pole_ids", []) or []}
    for placement in plan.get("placements", []) or []:
        if not isinstance(placement, dict):
            continue
        if str(placement.get("id", "")) not in pole_ids:
            continue
        px = _number(placement.get("x"))
        py = _number(placement.get("y"))
        if px is None or py is None:
            continue
        if math.hypot(x - px, y - py) <= _LOCAL_POLE_RADIUS + 1e-9:
            return True
    return False


def _architectural_mutation_authorized(
    name: str,
    arguments: dict[str, Any],
    plan: dict[str, Any],
) -> tuple[bool, str]:
    # Keep every previously-safe exact mutation. In particular this preserves strict
    # exact removals and exact splitter taps.
    allowed, reason = _STRICT_MUTATION_AUTHORIZED(name, arguments, plan)
    if allowed or name != "place_entity":
        return allowed, reason

    entity_name = str(arguments.get("entityName") or arguments.get("entity_name") or "")
    x = _number(arguments.get("x"))
    y = _number(arguments.get("y"))
    if not entity_name or x is None or y is None:
        return False, reason

    # Local belt geometry is intentionally adaptive. Splitters are excluded because taps
    # into an existing trunk are destructive/structural and remain exact-plan operations.
    if entity_name in {
        "transport-belt",
        "fast-transport-belt",
        "express-transport-belt",
        "underground-belt",
        "fast-underground-belt",
        "express-underground-belt",
    }:
        if _near_material_route(plan, entity_name, x, y):
            return True, "local additive logistics adaptation inside the validated route corridor"
        return False, "local belt adaptation is outside every validated material-route corridor"

    # A pole may move a few tiles around its planned location to avoid a live obstacle.
    # The pole type itself remains fixed by the validated power architecture.
    if entity_name in POLE_SPECS:
        if _near_planned_pole(plan, entity_name, x, y):
            return True, "local additive pole adaptation near a validated power location"
        return False, "local pole adaptation is outside the validated power corridor"

    # Machines, inserters, chests and splitters remain exact because moving those changes
    # production semantics rather than merely changing path geometry.
    return False, reason


def _install() -> None:
    guard._mutation_authorized = _architectural_mutation_authorized


_install()

if _PROMPT_MARKER not in base.SYSTEM_PROMPT:
    base.SYSTEM_PROMPT += """

ARCHITECTURAL PLAN EXECUTION POLICY:
Treat PLAN_VALID as an architectural contract, not an immutable list of every belt tile. Preserve the validated sources, production blocks, recipes, source taps, destructive removals, output intent, power source and throughput requirements. Exact source splitters and all removals remain strict because they can damage existing factory infrastructure.

During execution you MAY adapt ordinary transport-belt/underground-belt geometry locally around live obstacles, and you MAY shift a planned electric pole locally when needed. These are additive local routing decisions and do not require resubmitting the whole factory plan. If a belt placement returns invalid_position/collision, inspect only the nearby area, choose a short safe detour inside the same route corridor, and continue. Do not repeatedly retry an impossible coordinate.

A new submit_factory_plan is required only when the architecture changes: changing a material source or tap, moving/changing a production machine or inserter relationship, adding destructive removals, changing production capacity/district, or otherwise changing the validated factory topology. Local pathfinding around a furnace, pole, belt, cliff, or other obstacle is execution recovery, not architectural replanning.

This policy supersedes earlier exact-route wording for ordinary belts, underground belts and small local pole shifts only. It does not relax the no-manual-logistics rule, exact removal authorization, splitter tap safety, mutation budgets, or final live production verification.
"""
