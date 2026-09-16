from __future__ import annotations

import copy
import json
import math
from typing import Any, Awaitable, Callable

from .planning import POLE_SPECS


CallMcp = Callable[[str, dict[str, Any]], Awaitable[str]]
MAX_AUTO_POWER_BRIDGES = 16


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _point(value: Any) -> tuple[float, float] | None:
    if not isinstance(value, dict) or "x" not in value or "y" not in value:
        return None
    try:
        return float(value["x"]), float(value["y"])
    except (TypeError, ValueError):
        return None


def _parse_tool_json(output: str) -> dict[str, Any] | None:
    text = output.strip()
    if text.startswith(("MCP_TOOL_ERROR:", "MCP_TOOL_EXCEPTION:", "INVALID_TOOL_ARGUMENTS:")):
        return None
    try:
        value = json.loads(text)
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def _snap_half(value: float) -> float:
    return round(value * 2.0) / 2.0


def _power_nodes(plan: dict[str, Any]) -> tuple[tuple[float, float] | None, list[tuple[str, float, float]], float | None]:
    power = plan.get("power")
    placements = plan.get("placements")
    if not isinstance(power, dict) or not isinstance(placements, list):
        return None, [], None

    pole_type = str(power.get("pole_type", "")).strip()
    spec = POLE_SPECS.get(pole_type)
    if spec is None:
        return None, [], None
    anchor = _point(power.get("existing_anchor"))
    if anchor is None:
        return None, [], None

    by_id = {
        str(item.get("id", "")): item
        for item in placements
        if isinstance(item, dict) and str(item.get("id", ""))
    }
    nodes: list[tuple[str, float, float]] = []
    for pole_id in power.get("pole_ids", []) or []:
        placement = by_id.get(str(pole_id))
        if not isinstance(placement, dict):
            continue
        if str(placement.get("entity_name", "")) != pole_type:
            continue
        nodes.append((str(pole_id), _number(placement.get("x")), _number(placement.get("y"))))
    return anchor, nodes, float(spec[1])


def _connected_ids(plan: dict[str, Any]) -> set[str]:
    anchor, planned, wire_reach = _power_nodes(plan)
    if anchor is None or wire_reach is None:
        return set()

    nodes = [("__anchor__", anchor[0], anchor[1]), *planned]
    adjacency: dict[str, set[str]] = {node_id: set() for node_id, _, _ in nodes}
    for index, (a_id, ax, ay) in enumerate(nodes):
        for b_id, bx, by in nodes[index + 1 :]:
            if math.hypot(ax - bx, ay - by) <= wire_reach + 1e-9:
                adjacency[a_id].add(b_id)
                adjacency[b_id].add(a_id)

    seen = {"__anchor__"}
    pending = ["__anchor__"]
    while pending:
        current = pending.pop()
        for neighbour in adjacency.get(current, set()):
            if neighbour not in seen:
                seen.add(neighbour)
                pending.append(neighbour)
    return seen


def _candidate_offsets() -> list[tuple[float, float]]:
    offsets: list[tuple[float, float]] = [(0.0, 0.0)]
    for radius in (0.5, 1.0, 1.5, 2.0, 2.5):
        ring: list[tuple[float, float]] = []
        steps = int(radius * 2)
        for ix in range(-steps, steps + 1):
            for iy in range(-steps, steps + 1):
                dx = ix / 2.0
                dy = iy / 2.0
                if abs(max(abs(dx), abs(dy)) - radius) <= 1e-9:
                    ring.append((dx, dy))
        ring.sort(key=lambda item: item[0] * item[0] + item[1] * item[1])
        offsets.extend(ring)
    return offsets


def _unique_bridge_id(plan: dict[str, Any], sequence: int) -> str:
    existing = {
        str(item.get("id", ""))
        for item in (plan.get("placements") or [])
        if isinstance(item, dict)
    }
    base = f"auto-power-bridge-{sequence}"
    if base not in existing:
        return base
    suffix = 2
    while f"{base}-{suffix}" in existing:
        suffix += 1
    return f"{base}-{suffix}"


async def repair_power_network_disconnect(
    plan: Any,
    validation: Any,
    call_mcp: CallMcp,
) -> dict[str, Any] | None:
    """Conservatively add intermediate poles for a pure connectivity failure.

    This repair never moves or deletes model-planned entities. It is attempted only when
    every current validator issue is ``power_network_disconnected``. Candidate poles are
    checked against live Factorio placement before being added. The caller must run the
    complete deterministic validator again before treating the repaired plan as valid.
    """

    if not isinstance(plan, dict) or not isinstance(validation, dict):
        return None
    issues = [item for item in validation.get("issues", []) or [] if isinstance(item, dict)]
    if not issues or any(str(item.get("code", "")) != "power_network_disconnected" for item in issues):
        return None

    repaired = copy.deepcopy(plan)
    power = repaired.get("power")
    placements = repaired.get("placements")
    if not isinstance(power, dict) or not isinstance(placements, list):
        return None
    pole_type = str(power.get("pole_type", "")).strip()
    anchor, planned, wire_reach = _power_nodes(repaired)
    if not pole_type or anchor is None or wire_reach is None or not planned:
        return None

    existing_centers = {
        (_snap_half(_number(item.get("x"))), _snap_half(_number(item.get("y"))))
        for item in placements
        if isinstance(item, dict) and "x" in item and "y" in item
    }

    for sequence in range(1, MAX_AUTO_POWER_BRIDGES + 1):
        connected = _connected_ids(repaired)
        anchor_now, planned_now, wire_reach_now = _power_nodes(repaired)
        if anchor_now is None or wire_reach_now is None:
            return None
        disconnected = [node for node in planned_now if node[0] not in connected]
        if not disconnected:
            return repaired

        connected_nodes: list[tuple[str, float, float]] = [("__anchor__", anchor_now[0], anchor_now[1])]
        connected_nodes.extend(node for node in planned_now if node[0] in connected)
        source, target = min(
            (
                (a, b)
                for a in connected_nodes
                for b in disconnected
            ),
            key=lambda pair: math.hypot(pair[0][1] - pair[1][1], pair[0][2] - pair[1][2]),
        )
        distance = math.hypot(source[1] - target[1], source[2] - target[2])
        if distance <= wire_reach_now + 1e-9:
            # The graph should already have connected this pair; do not guess around an
            # inconsistent plan representation.
            return None

        # If one intermediate pole can bridge the whole gap, aim at the midpoint.
        # Otherwise advance about 80% of wire reach toward the disconnected component.
        step = distance / 2.0 if distance <= 2.0 * wire_reach_now else wire_reach_now * 0.80
        ux = (target[1] - source[1]) / distance
        uy = (target[2] - source[2]) / distance
        ideal_x = _snap_half(source[1] + ux * step)
        ideal_y = _snap_half(source[2] + uy * step)

        bridge_id = _unique_bridge_id(repaired, sequence)
        candidates: list[dict[str, Any]] = []
        for offset_index, (dx, dy) in enumerate(_candidate_offsets()):
            x = _snap_half(ideal_x + dx)
            y = _snap_half(ideal_y + dy)
            if (x, y) in existing_centers:
                continue
            if math.hypot(x - source[1], y - source[2]) > wire_reach_now - 0.05:
                continue
            if math.hypot(x - target[1], y - target[2]) >= distance - 0.1:
                continue
            candidates.append(
                {
                    "id": f"{bridge_id}-candidate-{offset_index}",
                    "entity_name": pole_type,
                    "x": x,
                    "y": y,
                    "direction": "north",
                }
            )
            if len(candidates) >= 80:
                break
        if not candidates:
            return None

        result = _parse_tool_json(
            await call_mcp(
                "check_entity_placement_batch",
                {"placementsJson": json.dumps(candidates, separators=(",", ":"))},
            )
        )
        if not result:
            return None
        by_id = {
            str(item.get("id", "")): item
            for item in (result.get("results") or [])
            if isinstance(item, dict)
        }
        chosen: dict[str, Any] | None = None
        for candidate in candidates:
            checked = by_id.get(str(candidate["id"]))
            if isinstance(checked, dict) and checked.get("prototype_exists", True) and checked.get("can_place") is True:
                chosen = candidate
                break
        if chosen is None:
            return None

        placement = {
            "id": bridge_id,
            "entity_name": pole_type,
            "x": chosen["x"],
            "y": chosen["y"],
            "direction": "north",
        }
        placements.append(placement)
        pole_ids = power.get("pole_ids")
        if not isinstance(pole_ids, list):
            pole_ids = []
            power["pole_ids"] = pole_ids
        pole_ids.append(bridge_id)
        existing_centers.add((float(chosen["x"]), float(chosen["y"])))

    return repaired if not [node for node in _power_nodes(repaired)[1] if node[0] not in _connected_ids(repaired)] else None
