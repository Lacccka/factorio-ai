from __future__ import annotations

import json
import math
from collections import defaultdict
from typing import Any, Awaitable, Callable

from . import app as base
from . import resilient_app as resilient


CallMcp = Callable[[str, dict[str, Any]], Awaitable[str]]
_PRIOR_VALIDATE_FACTORY_PLAN = base.validate_factory_plan
_MAX_REPORTED_ROUTE_CONFLICTS = 12
_BATCH_SIZE = 500
_UNDERGROUND_MAX_DISTANCE = {
    "underground-belt": 5,
    "fast-underground-belt": 7,
    "express-underground-belt": 9,
}


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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


def _key(x: float, y: float) -> tuple[int, int]:
    # Factorio entity centres commonly use half-tile coordinates. Quantising to tenths is
    # enough to make explicit placements and route points comparable without float noise.
    return round(x * 10), round(y * 10)


def _planned_underground_coverage(plan: dict[str, Any]) -> set[tuple[int, int]]:
    """Return surface tiles intentionally bypassed by planned underground-belt pairs.

    Material-route segments describe logical continuity, so a straight segment may span
    several surface tiles that are *not* meant to contain ordinary belts. Without this,
    preflighting every logical route tile would incorrectly reject a valid underground
    crossing over an existing belt/furnace. Explicit underground endpoints are validated
    by the normal placement validator; this helper only suppresses ordinary-belt checks
    for the covered span between a plausible pair.
    """

    groups: dict[tuple[str, str, int], list[float]] = defaultdict(list)
    for placement in plan.get("placements", []) or []:
        if not isinstance(placement, dict):
            continue
        name = str(placement.get("entity_name", ""))
        max_distance = _UNDERGROUND_MAX_DISTANCE.get(name)
        if max_distance is None:
            continue
        direction = str(placement.get("direction", "north")).lower()
        x = _number(placement.get("x"))
        y = _number(placement.get("y"))
        if x is None or y is None:
            continue
        if direction in {"east", "west"}:
            groups[(name, direction, round(y * 10))].append(x)
        elif direction in {"north", "south"}:
            groups[(name, direction, round(x * 10))].append(y)

    covered: set[tuple[int, int]] = set()
    for (name, direction, fixed), values in groups.items():
        values = sorted(set(values))
        max_center_distance = _UNDERGROUND_MAX_DISTANCE[name] + 1.0
        index = 0
        while index + 1 < len(values):
            first = values[index]
            second = values[index + 1]
            distance = abs(second - first)
            if distance < 1.0 - 0.05 or distance > max_center_distance + 0.05:
                index += 1
                continue
            steps = int(round(distance))
            step = 1.0 if second > first else -1.0
            for offset in range(steps + 1):
                variable = first + step * offset
                if direction in {"east", "west"}:
                    covered.add((round(variable * 10), fixed))
                else:
                    covered.add((fixed, round(variable * 10)))
            index += 2
    return covered


def _route_tiles(plan: dict[str, Any]) -> list[dict[str, Any]]:
    explicit_points: set[tuple[int, int]] = set()
    for placement in plan.get("placements", []) or []:
        if not isinstance(placement, dict):
            continue
        x = _number(placement.get("x"))
        y = _number(placement.get("y"))
        if x is not None and y is not None:
            explicit_points.add(_key(x, y))
    underground_coverage = _planned_underground_coverage(plan)

    tiles: list[dict[str, Any]] = []
    seen: set[tuple[str, int, int]] = set()
    for route in plan.get("material_routes", []) or []:
        if not isinstance(route, dict):
            continue
        belt = str(route.get("belt", "")).strip()
        if "transport-belt" not in belt:
            continue
        route_id = str(route.get("id", "")).strip()
        source = route.get("source") if isinstance(route.get("source"), dict) else {}
        source_x = _number(source.get("x"))
        source_y = _number(source.get("y"))
        tap_source = (
            str(route.get("source_mode", "")).lower() == "tap"
            and source_x is not None
            and source_y is not None
        )

        for segment_index, segment in enumerate(route.get("segments", []) or []):
            if not isinstance(segment, dict):
                continue
            start = segment.get("from") if isinstance(segment.get("from"), dict) else {}
            end = segment.get("to") if isinstance(segment.get("to"), dict) else {}
            x1 = _number(start.get("x"))
            y1 = _number(start.get("y"))
            x2 = _number(end.get("x"))
            y2 = _number(end.get("y"))
            if None in {x1, y1, x2, y2}:
                continue
            assert x1 is not None and y1 is not None and x2 is not None and y2 is not None
            dx = x2 - x1
            dy = y2 - y1
            if abs(dx) > 0.05 and abs(dy) > 0.05:
                continue
            distance = abs(dx) + abs(dy)
            steps = int(round(distance))
            if steps < 0 or abs(distance - steps) > 0.05:
                continue
            step_x = 0.0 if abs(dx) <= 0.05 else math.copysign(1.0, dx)
            step_y = 0.0 if abs(dy) <= 0.05 else math.copysign(1.0, dy)
            direction = str(segment.get("direction", "north")).lower()

            for step in range(steps + 1):
                x = x1 + step_x * step
                y = y1 + step_y * step
                point_key = _key(x, y)
                if tap_source and abs(x - source_x) <= 0.05 and abs(y - source_y) <= 0.05:
                    # The source lane is occupied by the validated tap splitter after replacement.
                    continue
                if point_key in explicit_points:
                    # Splitters/underground endpoints/etc. are preflighted as explicit placements.
                    continue
                if point_key in underground_coverage:
                    # Logical route continuity crosses below the surface here; no ordinary belt tile is intended.
                    continue
                unique = (belt, *point_key)
                if unique in seen:
                    continue
                seen.add(unique)
                tiles.append(
                    {
                        "id": f"route:{route_id}:{segment_index}:{step}",
                        "route_id": route_id,
                        "entity_name": belt,
                        "x": x,
                        "y": y,
                        "direction": direction,
                    }
                )
    return tiles


async def _nearby_exact(call_mcp: CallMcp, x: float, y: float) -> dict[str, Any] | None:
    payload = _parse_json(
        await call_mcp(
            "get_nearby_entities",
            {"radius": 1.0, "centerX": x, "centerY": y},
        )
    )
    if not isinstance(payload, dict):
        return None
    for entity in payload.get("entities", []) or []:
        if not isinstance(entity, dict):
            continue
        ex = _number(entity.get("x"))
        ey = _number(entity.get("y"))
        if ex is not None and ey is not None and abs(ex - x) <= 0.11 and abs(ey - y) <= 0.11:
            return entity
    return None


async def _validate_route_tile_preflight(
    plan: Any,
    call_mcp: CallMcp,
    available_tool_names: set[str],
) -> dict[str, Any]:
    validation = await _PRIOR_VALIDATE_FACTORY_PLAN(plan, call_mcp, available_tool_names)
    if not isinstance(validation, dict):
        validation = {"valid": False, "status": "PLAN_INVALID", "issues": [], "warnings": []}
    if not isinstance(plan, dict) or "check_entity_placement_batch" not in available_tool_names:
        return validation

    tiles = _route_tiles(plan)
    if not tiles:
        return validation

    blocked: list[dict[str, Any]] = []
    tile_by_id = {item["id"]: item for item in tiles}
    for offset in range(0, len(tiles), _BATCH_SIZE):
        chunk = tiles[offset : offset + _BATCH_SIZE]
        request = [
            {
                "id": item["id"],
                "entity_name": item["entity_name"],
                "x": item["x"],
                "y": item["y"],
                "direction": item["direction"],
            }
            for item in chunk
        ]
        result = _parse_json(
            await call_mcp(
                "check_entity_placement_batch",
                {"placementsJson": json.dumps(request, separators=(",", ":"))},
            )
        )
        if not isinstance(result, dict):
            issues = validation.setdefault("issues", [])
            if isinstance(issues, list):
                issues.append(
                    {
                        "code": "route_preflight_failed",
                        "message": "Could not preflight ordinary belt tiles for material routes.",
                    }
                )
            return _finish(validation)
        for entry in result.get("results", []) or []:
            if not isinstance(entry, dict) or entry.get("can_place") is not False:
                continue
            tile = tile_by_id.get(str(entry.get("id", "")))
            if tile is not None:
                blocked.append(tile)

    issues = validation.setdefault("issues", [])
    if not isinstance(issues, list):
        issues = []
        validation["issues"] = issues

    conflicts = 0
    for tile in blocked:
        if conflicts >= _MAX_REPORTED_ROUTE_CONFLICTS:
            break
        existing = None
        if "get_nearby_entities" in available_tool_names:
            existing = await _nearby_exact(call_mcp, float(tile["x"]), float(tile["y"]))
        if isinstance(existing, dict):
            existing_name = str(existing.get("name", ""))
            existing_direction = str(existing.get("direction", "")).lower()
            if (
                existing_name == str(tile["entity_name"])
                and (not existing_direction or existing_direction == str(tile["direction"]).lower())
            ):
                # Partial execution/resume: the exact route belt already exists and is satisfied.
                continue
            message = (
                f"Route {tile['route_id']} ordinary belt tile at ({tile['x']},{tile['y']}) "
                f"collides with live {existing_name or 'entity'}; reroute locally before PLAN_VALID."
            )
            existing_summary = {
                "name": existing_name,
                "direction": existing.get("direction"),
                "x": existing.get("x"),
                "y": existing.get("y"),
            }
        else:
            message = (
                f"Route {tile['route_id']} ordinary belt tile at ({tile['x']},{tile['y']}) "
                "cannot be placed in the live world; reroute locally before PLAN_VALID."
            )
            existing_summary = None
        issues.append(
            {
                "code": "route_tile_collision",
                "message": message,
                "route_id": tile["route_id"],
                "x": tile["x"],
                "y": tile["y"],
                "belt": tile["entity_name"],
                "direction": tile["direction"],
                "existing": existing_summary,
            }
        )
        conflicts += 1

    if len(blocked) > conflicts and conflicts >= _MAX_REPORTED_ROUTE_CONFLICTS:
        issues.append(
            {
                "code": "route_tile_collision_truncated",
                "message": "Additional route-tile conflicts exist; fix the reported local corridor first and resubmit.",
                "blocked_count": len(blocked),
                "reported_count": conflicts,
            }
        )
    return _finish(validation)


base.validate_factory_plan = _validate_route_tile_preflight
resilient.validate_factory_plan = _validate_route_tile_preflight
