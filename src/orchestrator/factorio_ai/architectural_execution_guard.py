from __future__ import annotations

import math
from typing import Any

from . import app as base
from . import plan_execution_guard as guard
from .planning import POLE_SPECS


# PLAN_VALID protects architecture and destructive operations. Additive local logistics
# must stay flexible enough for the reasoning model to respond to the live factory instead
# of replaying a brittle list of old coordinates.
_STRICT_MUTATION_AUTHORIZED = guard._mutation_authorized
_PROMPT_MARKER = "ARCHITECTURAL EXECUTION AUTONOMY"
_ROUTE_ENVELOPE_MARGIN = 16.0
_POWER_ENVELOPE_MARGIN = 12.0

_BELT_ENTITIES = {
    "transport-belt",
    "fast-transport-belt",
    "express-transport-belt",
    "underground-belt",
    "fast-underground-belt",
    "express-underground-belt",
}


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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


def _route_points(route: dict[str, Any]) -> list[tuple[float, float]]:
    result: list[tuple[float, float]] = []
    for key in ("source", "sink"):
        point = route.get(key)
        if not isinstance(point, dict):
            continue
        x = _number(point.get("x"))
        y = _number(point.get("y"))
        if x is not None and y is not None:
            result.append((x, y))
    for segment in route.get("segments", []) or []:
        if not isinstance(segment, dict):
            continue
        for key in ("from", "to"):
            point = segment.get(key)
            if not isinstance(point, dict):
                continue
            x = _number(point.get("x"))
            y = _number(point.get("y"))
            if x is not None and y is not None:
                result.append((x, y))
    return result


def _inside_envelope(points: list[tuple[float, float]], x: float, y: float, margin: float) -> bool:
    if not points:
        return False
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return (
        min(xs) - margin <= x <= max(xs) + margin
        and min(ys) - margin <= y <= max(ys) + margin
    )


def _inside_material_route_envelope(
    plan: dict[str, Any],
    entity_name: str,
    x: float,
    y: float,
) -> bool:
    for route in plan.get("material_routes", []) or []:
        if not isinstance(route, dict) or not _route_accepts_entity(route, entity_name):
            continue
        if _inside_envelope(_route_points(route), x, y, _ROUTE_ENVELOPE_MARGIN):
            return True
    return False


def _power_points(plan: dict[str, Any]) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    power = plan.get("power") if isinstance(plan.get("power"), dict) else {}
    anchor = power.get("existing_anchor") if isinstance(power.get("existing_anchor"), dict) else {}
    ax = _number(anchor.get("x"))
    ay = _number(anchor.get("y"))
    if ax is not None and ay is not None:
        points.append((ax, ay))

    pole_ids = {str(value) for value in power.get("pole_ids", []) or []}
    for placement in plan.get("placements", []) or []:
        if not isinstance(placement, dict):
            continue
        placement_id = str(placement.get("id", ""))
        # Include planned poles plus production-area placements so the model can bridge
        # coverage around machines without being trapped at an exact stale pole coordinate.
        if placement_id not in pole_ids and not str(placement.get("block_id", "")):
            continue
        x = _number(placement.get("x"))
        y = _number(placement.get("y"))
        if x is not None and y is not None:
            points.append((x, y))
    return points


def _architectural_mutation_authorized(
    name: str,
    arguments: dict[str, Any],
    plan: dict[str, Any],
) -> tuple[bool, str]:
    # Exact safe operations still pass through unchanged. This keeps validated source
    # splitters, removals, recipes, machines and inserters strict.
    allowed, reason = _STRICT_MUTATION_AUTHORIZED(name, arguments, plan)
    if allowed or name != "place_entity":
        return allowed, reason

    entity_name = str(arguments.get("entityName") or arguments.get("entity_name") or "")
    x = _number(arguments.get("x"))
    y = _number(arguments.get("y"))
    if not entity_name or x is None or y is None:
        return False, reason

    # Route geometry is intentionally not tied to the old polyline. The source/sink and
    # route item/tier remain architectural; Sol may choose a materially different local
    # path anywhere inside a generous envelope around that route.
    if entity_name in _BELT_ENTITIES:
        if _inside_material_route_envelope(plan, entity_name, x, y):
            return True, "additive logistics placement inside the validated route work area"
        return False, "additive logistics placement is outside the validated route work area"

    # Pole coordinates are execution geometry as long as the validated pole type and
    # production/power work area are preserved.
    if entity_name in POLE_SPECS:
        power = plan.get("power") if isinstance(plan.get("power"), dict) else {}
        planned_type = str(power.get("pole_type", "")).strip()
        if entity_name != planned_type:
            return False, "electric-pole type differs from the validated power architecture"
        if _inside_envelope(_power_points(plan), x, y, _POWER_ENVELOPE_MARGIN):
            return True, "additive pole placement inside the validated power work area"
        return False, "additive pole placement is outside the validated power work area"

    return False, reason


def _install() -> None:
    # The strict guard remains the authority for destructive/structural operations, while
    # every caller that consults guard._mutation_authorized now sees the adaptive policy.
    guard._mutation_authorized = _architectural_mutation_authorized

    # plan_execution_guard was written for the earlier exact-path architecture and appends
    # a contradictory instruction block. Remove that legacy tail before adding the single
    # execution policy used by the current runtime.
    legacy_marker = "\n\nVALIDATED EXECUTION CONFORMANCE POLICY:"
    if legacy_marker in base.SYSTEM_PROMPT:
        base.SYSTEM_PROMPT = base.SYSTEM_PROMPT.split(legacy_marker, 1)[0]


_install()

if _PROMPT_MARKER not in base.SYSTEM_PROMPT:
    base.SYSTEM_PROMPT += """

ARCHITECTURAL EXECUTION AUTONOMY:
PLAN_VALID freezes architecture, not ordinary path geometry. Preserve sources, taps, production blocks, recipes, throughput intent, destructive removals and output semantics. Ordinary belts, matching underground belts and the validated electric-pole type may move within their work areas when live geometry demands it. A local detour is not a replan.

Do not follow an old route polyline blindly through an occupied factory. Before entering a dense existing belt/production area, inspect or trace the local topology, choose a safe path, then build it. Existing splitter taps and arbitrary existing entities remain protected. Never modify unrelated pre-existing production to make a route fit.
"""
