from __future__ import annotations

import json
import math
from collections import defaultdict, deque
from typing import Any, Awaitable, Callable


PLAN_TOOL_NAME = "submit_factory_plan"

PLAN_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "name": PLAN_TOOL_NAME,
    "description": (
        "Submit a structured factory expansion plan for deterministic validation before any world-changing tools are unlocked. "
        "The validator checks recipe/machine rates, belt and lane throughput including burner-fuel demand, route direction/continuity, "
        "planned placement collisions, live can-place feasibility, inserter pickup/drop geometry, and electric-pole coverage/connectivity. "
        "If validation fails, correct the reported issues and resubmit. Do not mutate the world until this tool returns valid=true."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "plan": {
                "type": "object",
                "properties": {
                    "production_blocks": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "recipe": {"type": "string"},
                                "machine": {"type": "string"},
                                "machine_count": {"type": "integer", "minimum": 1},
                                "target_item": {"type": "string"},
                                "target_rate_per_second": {"type": "number", "minimum": 0},
                            },
                            "required": ["id", "recipe", "machine", "machine_count"],
                        },
                    },
                    "material_routes": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "item": {"type": "string"},
                                "role": {"type": "string", "enum": ["input", "output", "fuel"]},
                                "belt": {"type": "string"},
                                "lane": {"type": "string", "enum": ["left", "right", "both"]},
                                "source_mode": {"type": "string", "enum": ["tap", "extend", "new"]},
                                "source": {
                                    "type": "object",
                                    "properties": {"x": {"type": "number"}, "y": {"type": "number"}},
                                    "required": ["x", "y"],
                                },
                                "sink": {
                                    "type": "object",
                                    "properties": {"x": {"type": "number"}, "y": {"type": "number"}},
                                    "required": ["x", "y"],
                                },
                                "segments": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
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
                                        "required": ["from", "to", "direction"],
                                    },
                                },
                                "feeds_blocks": {"type": "array", "items": {"type": "string"}},
                                "source_blocks": {"type": "array", "items": {"type": "string"}},
                            },
                            "required": ["id", "item", "role", "belt", "lane", "source_mode", "source", "sink", "segments"],
                        },
                    },
                    "placements": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "entity_name": {"type": "string"},
                                "x": {"type": "number"},
                                "y": {"type": "number"},
                                "direction": {"type": "string"},
                                "block_id": {"type": "string"},
                                "pickup_ref": {"type": "string"},
                                "drop_ref": {"type": "string"},
                                "allow_replace_existing": {"type": "boolean"},
                            },
                            "required": ["id", "entity_name", "x", "y", "direction"],
                        },
                    },
                    "power": {
                        "type": "object",
                        "properties": {
                            "pole_type": {"type": "string"},
                            "pole_ids": {"type": "array", "items": {"type": "string"}},
                            "existing_anchor": {
                                "type": "object",
                                "properties": {"x": {"type": "number"}, "y": {"type": "number"}},
                                "required": ["x", "y"],
                            },
                        },
                        "required": ["pole_type", "pole_ids", "existing_anchor"],
                    },
                },
                "required": ["production_blocks", "material_routes", "placements", "power"],
            }
        },
        "required": ["plan"],
    },
}


BELT_CAPACITY_ITEMS_PER_SECOND = {
    "transport-belt": 15.0,
    "fast-transport-belt": 30.0,
    "express-transport-belt": 45.0,
}

# Factorio base-game pole geometry used by upstream PowerPoleLayoutService.
POLE_SPECS = {
    "small-electric-pole": (2.5, 7.5),
    "medium-electric-pole": (3.5, 9.0),
    "big-electric-pole": (2.0, 30.0),
    "substation": (9.0, 18.0),
}

CallMcp = Callable[[str, dict[str, Any]], Awaitable[str]]


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _point(value: Any) -> tuple[float, float] | None:
    if not isinstance(value, dict):
        return None
    if "x" not in value or "y" not in value:
        return None
    try:
        return float(value["x"]), float(value["y"])
    except (TypeError, ValueError):
        return None


def _same_point(a: tuple[float, float], b: tuple[float, float], tolerance: float = 0.05) -> bool:
    return abs(a[0] - b[0]) <= tolerance and abs(a[1] - b[1]) <= tolerance


def _parse_json_tool_output(output: str) -> dict[str, Any] | None:
    text = output.strip()
    if text.startswith("MCP_TOOL_ERROR:") or text.startswith("MCP_TOOL_EXCEPTION:"):
        return None
    try:
        value = json.loads(text)
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def _entity_needs_power(name: str) -> bool:
    lowered = name.lower()
    passive_tokens = (
        "transport-belt",
        "splitter",
        "underground-belt",
        "pipe",
        "wall",
        "gate",
        "stone-furnace",
        "steel-furnace",
        "burner",
        "wooden-chest",
        "iron-chest",
        "steel-chest",
        "electric-pole",
        "substation",
        "accumulator",
    )
    if any(token in lowered for token in passive_tokens):
        return False
    return any(
        token in lowered
        for token in (
            "assembling-machine",
            "electric-furnace",
            "inserter",
            "lab",
            "mining-drill",
            "pump",
            "beacon",
            "roboport",
            "radar",
        )
    )


def _direction_vector(direction: str) -> tuple[int, int] | None:
    return {
        "north": (0, -1),
        "south": (0, 1),
        "east": (1, 0),
        "west": (-1, 0),
    }.get(direction.lower())


def _point_on_segment(point: tuple[float, float], segment: dict[str, Any], tolerance: float = 0.55) -> bool:
    start = _point(segment.get("from"))
    end = _point(segment.get("to"))
    if start is None or end is None:
        return False
    px, py = point
    x1, y1 = start
    x2, y2 = end
    if abs(x1 - x2) <= 0.05:
        return abs(px - x1) <= tolerance and min(y1, y2) - tolerance <= py <= max(y1, y2) + tolerance
    if abs(y1 - y2) <= 0.05:
        return abs(py - y1) <= tolerance and min(x1, x2) - tolerance <= px <= max(x1, x2) + tolerance
    return False


def _inside_box(point: tuple[float, float], box: tuple[float, float, float, float], tolerance: float = 0.05) -> bool:
    x, y = point
    x1, y1, x2, y2 = box
    return x1 - tolerance <= x <= x2 + tolerance and y1 - tolerance <= y <= y2 + tolerance


def _boxes_overlap(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    # Tile-aligned entity footprints touching at an edge are fine; positive overlap is not.
    return min(a[2], b[2]) - max(a[0], b[0]) > 0.01 and min(a[3], b[3]) - max(a[1], b[1]) > 0.01


async def validate_factory_plan(
    plan: Any,
    call_mcp: CallMcp,
    available_tool_names: set[str],
) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    def issue(code: str, message: str, **extra: Any) -> None:
        issues.append({"code": code, "message": message, **extra})

    def warning(code: str, message: str, **extra: Any) -> None:
        warnings.append({"code": code, "message": message, **extra})

    if not isinstance(plan, dict):
        return {"valid": False, "issues": [{"code": "invalid_plan", "message": "plan must be an object"}], "warnings": []}

    blocks = plan.get("production_blocks")
    routes = plan.get("material_routes")
    placements = plan.get("placements")
    power = plan.get("power")
    if not isinstance(blocks, list) or not blocks:
        issue("missing_production_blocks", "production_blocks must be a non-empty array")
        blocks = []
    if not isinstance(routes, list) or not routes:
        issue("missing_material_routes", "material_routes must be a non-empty array")
        routes = []
    if not isinstance(placements, list) or not placements:
        issue("missing_placements", "placements must be a non-empty array")
        placements = []
    if not isinstance(power, dict):
        issue("missing_power_plan", "power must be an object")
        power = {}

    required_mcp = {"get_recipe_details", "get_entity_prototype", "check_entity_placement_batch"}
    missing_tools = sorted(required_mcp - available_tool_names)
    if missing_tools:
        issue("validator_tool_missing", f"Required MCP validation tools are unavailable: {', '.join(missing_tools)}")
        return {"valid": False, "issues": issues, "warnings": warnings}

    block_ids: set[str] = set()
    block_input_rates: dict[tuple[str, str], float] = {}
    block_output_rates: dict[tuple[str, str], float] = {}
    block_machine_meta: dict[str, dict[str, Any]] = {}
    block_summary: list[dict[str, Any]] = []
    prototype_cache: dict[str, dict[str, Any]] = {}
    fuel_info_cache: dict[str, dict[str, Any]] = {}

    async def get_prototype(name: str) -> dict[str, Any] | None:
        if name in prototype_cache:
            return prototype_cache[name]
        payload = _parse_json_tool_output(await call_mcp("get_entity_prototype", {"entityName": name}))
        if payload is not None:
            prototype_cache[name] = payload
        return payload

    async def get_fuel_info(name: str) -> dict[str, Any] | None:
        if name in fuel_info_cache:
            return fuel_info_cache[name]
        if "get_item_fuel_info" not in available_tool_names:
            return None
        payload = _parse_json_tool_output(await call_mcp("get_item_fuel_info", {"itemName": name}))
        if payload is not None:
            fuel_info_cache[name] = payload
        return payload

    for raw_block in blocks:
        if not isinstance(raw_block, dict):
            issue("invalid_block", "Every production block must be an object")
            continue
        block_id = str(raw_block.get("id", "")).strip()
        recipe_name = str(raw_block.get("recipe", "")).strip()
        machine = str(raw_block.get("machine", "")).strip()
        try:
            machine_count = int(raw_block.get("machine_count", 0))
        except (TypeError, ValueError):
            machine_count = 0
        if not block_id or block_id in block_ids:
            issue("invalid_block_id", f"Production block id must be unique and non-empty: {block_id!r}")
            continue
        block_ids.add(block_id)
        if not recipe_name or not machine or machine_count <= 0:
            issue("invalid_block_fields", f"Block {block_id} requires recipe, machine and machine_count > 0", block_id=block_id)
            continue

        recipe = _parse_json_tool_output(await call_mcp("get_recipe_details", {"recipe": recipe_name}))
        prototype = await get_prototype(machine)
        if not recipe or recipe.get("success") is False:
            issue("recipe_lookup_failed", f"Could not read recipe {recipe_name} for block {block_id}", block_id=block_id)
            continue
        if not prototype or prototype.get("success") is False:
            issue("prototype_lookup_failed", f"Could not read prototype {machine} for block {block_id}", block_id=block_id)
            continue

        has_burner = prototype.get("has_burner") is True
        burner_effectivity = _number(prototype.get("burner_effectivity"), 1.0)
        if burner_effectivity <= 0:
            burner_effectivity = 1.0
        fuel_categories = {
            str(value)
            for value in (prototype.get("burner_fuel_categories") or [])
            if str(value)
        }
        block_machine_meta[block_id] = {
            "machine": machine,
            "machine_count": machine_count,
            "energy_usage_per_tick": _number(prototype.get("energy_usage")),
            "has_burner": has_burner,
            "burner_effectivity": burner_effectivity,
            "burner_fuel_categories": fuel_categories,
        }

        energy = _number(recipe.get("energy"))
        crafting_speed = _number(prototype.get("crafting_speed"))
        if energy <= 0 or crafting_speed <= 0:
            issue(
                "invalid_recipe_or_machine_rate",
                f"Block {block_id} has energy={energy} and crafting_speed={crafting_speed}; cannot compute rate",
                block_id=block_id,
            )
            continue

        cycles_per_second = machine_count * crafting_speed / energy
        for ingredient in recipe.get("ingredients", []) or []:
            if isinstance(ingredient, dict) and ingredient.get("type", "item") == "item":
                item = str(ingredient.get("name", ""))
                if item:
                    block_input_rates[(block_id, item)] = block_input_rates.get((block_id, item), 0.0) + _number(ingredient.get("amount")) * cycles_per_second
        for product in recipe.get("products", []) or []:
            if isinstance(product, dict) and product.get("type", "item") == "item":
                item = str(product.get("name", ""))
                if item:
                    probability = _number(product.get("probability"), 1.0) or 1.0
                    block_output_rates[(block_id, item)] = block_output_rates.get((block_id, item), 0.0) + _number(product.get("amount")) * probability * cycles_per_second

        target_item = str(raw_block.get("target_item", "")).strip()
        if not target_item:
            products = [p for p in recipe.get("products", []) or [] if isinstance(p, dict) and p.get("type", "item") == "item"]
            target_item = str(products[0].get("name", "")) if products else ""
        actual_rate = block_output_rates.get((block_id, target_item), 0.0)
        target_rate = _number(raw_block.get("target_rate_per_second"))
        if target_rate > 0 and actual_rate + 1e-9 < target_rate:
            issue(
                "production_target_exceeds_capacity",
                f"Block {block_id} can produce {actual_rate:.3f}/s of {target_item}, below requested {target_rate:.3f}/s",
                block_id=block_id,
                actual_rate_per_second=round(actual_rate, 6),
                target_rate_per_second=round(target_rate, 6),
            )
        block_summary.append(
            {
                "id": block_id,
                "recipe": recipe_name,
                "machine": machine,
                "machine_count": machine_count,
                "crafting_speed": crafting_speed,
                "cycles_per_second": round(cycles_per_second, 6),
                "target_item": target_item,
                "actual_output_rate_per_second": round(actual_rate, 6),
                "has_burner": has_burner,
            }
        )

    route_ids: set[str] = set()
    route_map: dict[str, dict[str, Any]] = {}
    route_summary: list[dict[str, Any]] = []
    fuel_routed_blocks: set[str] = set()
    for route in routes:
        if not isinstance(route, dict):
            issue("invalid_route", "Every material route must be an object")
            continue
        route_id = str(route.get("id", "")).strip()
        if not route_id or route_id in route_ids:
            issue("invalid_route_id", f"Material route id must be unique and non-empty: {route_id!r}")
            continue
        route_ids.add(route_id)
        route_map[route_id] = route
        item = str(route.get("item", "")).strip()
        role = str(route.get("role", "")).strip().lower()
        belt = str(route.get("belt", "")).strip()
        lane = str(route.get("lane", "")).strip().lower()
        source_mode = str(route.get("source_mode", "")).strip().lower()
        source = _point(route.get("source"))
        sink = _point(route.get("sink"))
        segments = route.get("segments")
        if not item or role not in {"input", "output", "fuel"} or lane not in {"left", "right", "both"} or source_mode not in {"tap", "extend", "new"}:
            issue("invalid_route_fields", f"Route {route_id} has invalid item/role/lane/source_mode", route_id=route_id)
            continue
        if source is None or sink is None or not isinstance(segments, list) or not segments:
            issue("invalid_route_geometry", f"Route {route_id} requires source, sink and at least one segment", route_id=route_id)
            continue
        full_capacity = BELT_CAPACITY_ITEMS_PER_SECOND.get(belt)
        if full_capacity is None:
            issue("unknown_belt_capacity", f"Route {route_id} uses unsupported belt {belt}", route_id=route_id)
            continue
        capacity = full_capacity if lane == "both" else full_capacity / 2.0

        required_rate = 0.0
        refs: list[str]
        if role in {"input", "fuel"}:
            refs = [str(v) for v in route.get("feeds_blocks", []) or []]
            if not refs:
                issue("route_missing_consumers", f"{role.capitalize()} route {route_id} must list feeds_blocks", route_id=route_id)
            if role == "input":
                for block_id in refs:
                    if block_id not in block_ids:
                        issue("unknown_route_block", f"Route {route_id} references unknown block {block_id}", route_id=route_id)
                    required_rate += block_input_rates.get((block_id, item), 0.0)
            else:
                if "get_item_fuel_info" not in available_tool_names:
                    issue(
                        "validator_tool_missing",
                        f"Fuel route {route_id} requires get_item_fuel_info from the patched FactorioMCP",
                        route_id=route_id,
                    )
                fuel_info = await get_fuel_info(item)
                fuel_value = 0.0 if not fuel_info else _number(fuel_info.get("fuel_value"))
                fuel_category = "" if not fuel_info else str(fuel_info.get("fuel_category") or "")
                if not fuel_info or fuel_info.get("success") is False or fuel_value <= 0:
                    issue("invalid_fuel_item", f"Route {route_id} item {item} is not a usable fuel", route_id=route_id)
                for block_id in refs:
                    if block_id not in block_ids:
                        issue("unknown_route_block", f"Route {route_id} references unknown block {block_id}", route_id=route_id)
                        continue
                    meta = block_machine_meta.get(block_id)
                    if not meta:
                        continue
                    if not meta.get("has_burner"):
                        issue(
                            "fuel_route_to_non_burner_machine",
                            f"Fuel route {route_id} feeds block {block_id}, but its machine is not burner-powered",
                            route_id=route_id,
                            block_id=block_id,
                        )
                        continue
                    accepted_categories = meta.get("burner_fuel_categories") or set()
                    if fuel_category and accepted_categories and fuel_category not in accepted_categories:
                        issue(
                            "fuel_category_mismatch",
                            f"Fuel route {route_id} uses category {fuel_category}, not accepted by block {block_id}",
                            route_id=route_id,
                            block_id=block_id,
                        )
                        continue
                    energy_usage_per_tick = _number(meta.get("energy_usage_per_tick"))
                    if energy_usage_per_tick <= 0 or fuel_value <= 0:
                        issue(
                            "fuel_rate_unavailable",
                            f"Cannot calculate fuel demand for block {block_id}: energy_usage={energy_usage_per_tick}, fuel_value={fuel_value}",
                            route_id=route_id,
                            block_id=block_id,
                        )
                        continue
                    machine_count = int(meta.get("machine_count") or 0)
                    effectivity = max(_number(meta.get("burner_effectivity"), 1.0), 1e-9)
                    # Factorio runtime energy_usage is joules/tick; 60 ticks = one second.
                    required_rate += machine_count * energy_usage_per_tick * 60.0 / (fuel_value * effectivity)
                    fuel_routed_blocks.add(block_id)
        else:
            refs = [str(v) for v in route.get("source_blocks", []) or []]
            if not refs:
                issue("route_missing_producers", f"Output route {route_id} must list source_blocks", route_id=route_id)
            for block_id in refs:
                if block_id not in block_ids:
                    issue("unknown_route_block", f"Route {route_id} references unknown block {block_id}", route_id=route_id)
                required_rate += block_output_rates.get((block_id, item), 0.0)

        if required_rate <= 0:
            rate_kind = "fuel demand" if role == "fuel" else ("input" if role == "input" else "output")
            issue(
                "route_has_no_matching_material_rate",
                f"Route {route_id} carries {item} but referenced blocks have no matching {rate_kind} rate",
                route_id=route_id,
            )
        elif required_rate > capacity + 1e-9:
            issue(
                "belt_capacity_exceeded",
                f"Route {route_id} requires {required_rate:.3f} {item}/s but {belt} lane={lane} carries at most {capacity:.3f}/s",
                route_id=route_id,
                required_rate_per_second=round(required_rate, 6),
                capacity_per_second=capacity,
            )

        previous_end: tuple[float, float] | None = None
        first_start: tuple[float, float] | None = None
        last_end: tuple[float, float] | None = None
        for index, segment in enumerate(segments):
            if not isinstance(segment, dict):
                issue("invalid_route_segment", f"Route {route_id} segment {index} is not an object", route_id=route_id)
                continue
            start = _point(segment.get("from"))
            end = _point(segment.get("to"))
            direction = str(segment.get("direction", "")).lower()
            vector = _direction_vector(direction)
            if start is None or end is None or vector is None:
                issue("invalid_route_segment", f"Route {route_id} segment {index} has invalid coordinates/direction", route_id=route_id)
                continue
            dx = end[0] - start[0]
            dy = end[1] - start[1]
            if abs(dx) > 0.05 and abs(dy) > 0.05:
                issue("diagonal_belt_segment", f"Route {route_id} segment {index} is diagonal", route_id=route_id)
            elif dx * vector[0] + dy * vector[1] <= 0.05:
                issue(
                    "belt_direction_mismatch",
                    f"Route {route_id} segment {index} does not travel {direction} from source to sink",
                    route_id=route_id,
                    segment=index,
                )
            if previous_end is not None and not _same_point(previous_end, start):
                issue("route_discontinuity", f"Route {route_id} segment {index} does not start where the previous segment ends", route_id=route_id)
            if first_start is None:
                first_start = start
            previous_end = end
            last_end = end
        if first_start is not None and not _same_point(first_start, source):
            issue("route_source_mismatch", f"Route {route_id} first segment does not start at declared source", route_id=route_id)
        if last_end is not None and not _same_point(last_end, sink):
            issue("route_sink_mismatch", f"Route {route_id} last segment does not end at declared sink", route_id=route_id)

        if source_mode in {"tap", "extend"} and "get_nearby_entities" in available_tool_names:
            nearby = _parse_json_tool_output(
                await call_mcp("get_nearby_entities", {"radius": 1.5, "centerX": source[0], "centerY": source[1]})
            )
            candidates = [] if not nearby else nearby.get("entities", []) or []
            exact_belts = [
                entity
                for entity in candidates
                if isinstance(entity, dict)
                and "belt" in str(entity.get("type", entity.get("name", ""))).lower()
                and abs(_number(entity.get("x")) - source[0]) <= 0.6
                and abs(_number(entity.get("y")) - source[1]) <= 0.6
            ]
            if not exact_belts:
                warning("route_source_not_confirmed", f"Could not confirm an existing belt at source of route {route_id}", route_id=route_id)
            elif source_mode == "extend" and segments:
                existing_direction = str(exact_belts[0].get("direction", "")).lower()
                first_direction = str(segments[0].get("direction", "")).lower() if isinstance(segments[0], dict) else ""
                if existing_direction and existing_direction != first_direction:
                    issue(
                        "existing_belt_extension_direction_mismatch",
                        f"Route {route_id} claims to extend an existing {existing_direction}-bound belt but first segment is {first_direction}",
                        route_id=route_id,
                        existing_direction=existing_direction,
                        planned_direction=first_direction,
                    )

        route_summary.append(
            {
                "id": route_id,
                "item": item,
                "role": role,
                "belt": belt,
                "lane": lane,
                "required_rate_per_second": round(required_rate, 6),
                "capacity_per_second": capacity,
            }
        )

    for block_id, meta in block_machine_meta.items():
        if meta.get("has_burner") and block_id not in fuel_routed_blocks:
            issue(
                "burner_block_missing_fuel_route",
                f"Burner-powered production block {block_id} has no validated role=fuel material route",
                block_id=block_id,
            )

    placement_ids: set[str] = set()
    placement_map: dict[str, dict[str, Any]] = {}
    placement_boxes: dict[str, tuple[float, float, float, float]] = {}
    placement_batch: list[dict[str, Any]] = []

    for placement in placements:
        if not isinstance(placement, dict):
            issue("invalid_placement", "Every placement must be an object")
            continue
        placement_id = str(placement.get("id", "")).strip()
        entity_name = str(placement.get("entity_name", "")).strip()
        direction = str(placement.get("direction", "north")).strip().lower()
        if not placement_id or placement_id in placement_ids:
            issue("invalid_placement_id", f"Placement id must be unique and non-empty: {placement_id!r}")
            continue
        placement_ids.add(placement_id)
        placement_map[placement_id] = placement
        try:
            x = float(placement["x"])
            y = float(placement["y"])
        except (KeyError, TypeError, ValueError):
            issue("invalid_placement_coordinates", f"Placement {placement_id} has invalid coordinates", placement_id=placement_id)
            continue
        if not entity_name:
            issue("invalid_placement_entity", f"Placement {placement_id} has no entity_name", placement_id=placement_id)
            continue
        prototype = await get_prototype(entity_name)
        if not prototype:
            issue("prototype_lookup_failed", f"Could not read prototype {entity_name} for placement {placement_id}", placement_id=placement_id)
            continue
        width = max(1.0, _number(prototype.get("tile_width"), 1.0))
        height = max(1.0, _number(prototype.get("tile_height"), 1.0))
        if direction in {"east", "west"}:
            width, height = height, width
        placement_boxes[placement_id] = (x - width / 2.0, y - height / 2.0, x + width / 2.0, y + height / 2.0)
        placement_batch.append({"id": placement_id, "entity_name": entity_name, "x": x, "y": y, "direction": direction})

    ids = list(placement_boxes)
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            if _boxes_overlap(placement_boxes[ids[i]], placement_boxes[ids[j]]):
                issue(
                    "planned_entity_overlap",
                    f"Placements {ids[i]} and {ids[j]} overlap",
                    placement_ids=[ids[i], ids[j]],
                )

    if placement_batch:
        batch_result = _parse_json_tool_output(
            await call_mcp("check_entity_placement_batch", {"placementsJson": json.dumps(placement_batch, separators=(",", ":"))})
        )
        if not batch_result:
            issue("placement_preflight_failed", "check_entity_placement_batch did not return valid JSON")
        else:
            by_id = {str(p.get("id", "")): p for p in placements if isinstance(p, dict)}
            for result in batch_result.get("results", []) or []:
                if not isinstance(result, dict):
                    continue
                placement_id = str(result.get("id", ""))
                if not result.get("prototype_exists", True):
                    issue("unknown_placement_prototype", f"Placement {placement_id} uses an unknown entity prototype", placement_id=placement_id)
                if result.get("can_place") is False and not bool(by_id.get(placement_id, {}).get("allow_replace_existing", False)):
                    issue(
                        "cannot_place_entity",
                        f"Factorio reports placement {placement_id} cannot be placed at the requested position",
                        placement_id=placement_id,
                        entity_name=result.get("entity_name"),
                        x=result.get("x"),
                        y=result.get("y"),
                    )

    # Inserter pickup/drop references make the geometry machine-checkable instead of relying on prose.
    for placement_id, placement in placement_map.items():
        entity_name = str(placement.get("entity_name", "")).lower()
        if "inserter" not in entity_name:
            continue
        pickup_ref = str(placement.get("pickup_ref", "")).strip()
        drop_ref = str(placement.get("drop_ref", "")).strip()
        if not pickup_ref or not drop_ref:
            issue(
                "inserter_missing_refs",
                f"Inserter {placement_id} must declare pickup_ref and drop_ref",
                placement_id=placement_id,
            )
            continue
        direction = str(placement.get("direction", "north")).lower()
        vector = _direction_vector(direction)
        if vector is None:
            issue("invalid_inserter_direction", f"Inserter {placement_id} has unsupported direction {direction}", placement_id=placement_id)
            continue
        reach = 2.0 if "long-handed" in entity_name else 1.0
        x = _number(placement.get("x"))
        y = _number(placement.get("y"))
        pickup_point = (x + vector[0] * reach, y + vector[1] * reach)
        drop_point = (x - vector[0] * reach, y - vector[1] * reach)

        def ref_contains(ref: str, point: tuple[float, float]) -> bool:
            if ref in placement_boxes:
                return _inside_box(point, placement_boxes[ref], tolerance=0.6)
            route = route_map.get(ref)
            if route is not None:
                return any(_point_on_segment(point, segment) for segment in route.get("segments", []) or [] if isinstance(segment, dict))
            return False

        if not ref_contains(pickup_ref, pickup_point):
            issue(
                "inserter_pickup_mismatch",
                f"Inserter {placement_id} pickup point {pickup_point} does not intersect declared ref {pickup_ref}",
                placement_id=placement_id,
                pickup_ref=pickup_ref,
            )
        if not ref_contains(drop_ref, drop_point):
            issue(
                "inserter_drop_mismatch",
                f"Inserter {placement_id} drop point {drop_point} does not intersect declared ref {drop_ref}",
                placement_id=placement_id,
                drop_ref=drop_ref,
            )

    pole_type = str(power.get("pole_type", "")).strip()
    pole_ids = [str(v) for v in power.get("pole_ids", []) or []]
    anchor = _point(power.get("existing_anchor"))
    spec = POLE_SPECS.get(pole_type)
    if not pole_type or spec is None:
        issue("invalid_pole_type", f"Unsupported pole_type {pole_type!r}")
    if anchor is None:
        issue("missing_power_anchor", "power.existing_anchor must identify a connected existing pole")

    planned_poles: list[tuple[str, float, float]] = []
    if spec is not None:
        for pole_id in pole_ids:
            placement = placement_map.get(pole_id)
            if not placement:
                issue("unknown_pole_placement", f"power.pole_ids references unknown placement {pole_id}", placement_id=pole_id)
                continue
            if str(placement.get("entity_name", "")) != pole_type:
                issue("pole_type_mismatch", f"Placement {pole_id} is not {pole_type}", placement_id=pole_id)
                continue
            planned_poles.append((pole_id, _number(placement.get("x")), _number(placement.get("y"))))

        supply_radius, wire_reach = spec
        for placement_id, placement in placement_map.items():
            name = str(placement.get("entity_name", ""))
            if not _entity_needs_power(name):
                continue
            x = _number(placement.get("x"))
            y = _number(placement.get("y"))
            covered = any(abs(x - px) < supply_radius and abs(y - py) < supply_radius for _, px, py in planned_poles)
            if not covered and anchor is not None:
                covered = abs(x - anchor[0]) < supply_radius and abs(y - anchor[1]) < supply_radius
            if not covered:
                issue(
                    "entity_without_power_coverage",
                    f"Electric placement {placement_id} is outside planned pole supply area",
                    placement_id=placement_id,
                )

        if anchor is not None and planned_poles:
            nodes = [("__anchor__", anchor[0], anchor[1]), *planned_poles]
            adjacency: dict[str, set[str]] = defaultdict(set)
            for i in range(len(nodes)):
                for j in range(i + 1, len(nodes)):
                    a_id, ax, ay = nodes[i]
                    b_id, bx, by = nodes[j]
                    if math.hypot(ax - bx, ay - by) <= wire_reach + 1e-9:
                        adjacency[a_id].add(b_id)
                        adjacency[b_id].add(a_id)
            seen = {"__anchor__"}
            queue: deque[str] = deque(["__anchor__"])
            while queue:
                current = queue.popleft()
                for neighbour in adjacency[current]:
                    if neighbour not in seen:
                        seen.add(neighbour)
                        queue.append(neighbour)
            disconnected = [pole_id for pole_id, _, _ in planned_poles if pole_id not in seen]
            if disconnected:
                issue(
                    "power_network_disconnected",
                    f"Planned poles are not connected to existing anchor: {', '.join(disconnected)}",
                    placement_ids=disconnected,
                )

        if anchor is not None and "get_nearby_entities" in available_tool_names:
            nearby = _parse_json_tool_output(
                await call_mcp("get_nearby_entities", {"radius": 1.5, "centerX": anchor[0], "centerY": anchor[1]})
            )
            entities = [] if not nearby else nearby.get("entities", []) or []
            anchor_found = any(
                isinstance(entity, dict)
                and ("electric-pole" in str(entity.get("type", "")) or "electric-pole" in str(entity.get("name", "")) or str(entity.get("name", "")) == "substation")
                and abs(_number(entity.get("x")) - anchor[0]) <= 0.6
                and abs(_number(entity.get("y")) - anchor[1]) <= 0.6
                for entity in entities
            )
            if not anchor_found:
                issue("power_anchor_not_found", "No existing electric pole was confirmed at power.existing_anchor")

    valid = not issues
    return {
        "valid": valid,
        "status": "PLAN_VALID" if valid else "PLAN_INVALID",
        "issue_count": len(issues),
        "warning_count": len(warnings),
        "issues": issues,
        "warnings": warnings,
        "production_blocks": block_summary,
        "material_routes": route_summary,
        "placement_count": len(placement_map),
        "message": (
            "Plan passed deterministic validation. Mutating tools may now be unlocked; execute this exact plan without redesigning it."
            if valid
            else "Plan failed deterministic validation. Correct the listed issues and resubmit before any mutation."
        ),
    }
