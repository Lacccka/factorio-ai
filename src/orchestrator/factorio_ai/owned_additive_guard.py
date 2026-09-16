from __future__ import annotations

import json
import math
from types import SimpleNamespace
from typing import Any

from . import app as base
from . import persistent_app as persistent
from . import plan_execution_guard as guard


_PRIOR_EXECUTE_FUNCTION_CALLS = base._execute_function_calls
_LOW_LEVEL_EXECUTE_FUNCTION_CALLS = guard._PRIOR_EXECUTE_FUNCTION_CALLS

_OWNABLE_ENTITIES = {
    "transport-belt",
    "fast-transport-belt",
    "express-transport-belt",
    "underground-belt",
    "fast-underground-belt",
    "express-underground-belt",
    "small-electric-pole",
    "medium-electric-pole",
    "big-electric-pole",
    "substation",
}


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _arguments(item: Any) -> dict[str, Any]:
    try:
        value = json.loads(getattr(item, "arguments", "") or "{}")
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _coords(arguments: dict[str, Any]) -> tuple[float, float] | None:
    x = _number(arguments.get("x", arguments.get("targetX")))
    y = _number(arguments.get("y", arguments.get("targetY")))
    if x is None or y is None:
        return None
    return x, y


def _same_point(a: dict[str, Any], x: float, y: float) -> bool:
    ax = _number(a.get("x", a.get("targetX")))
    ay = _number(a.get("y", a.get("targetY")))
    return ax is not None and ay is not None and math.hypot(ax - x, ay - y) <= 0.11


def _owned_additive_entity(metrics: base.RunMetrics, arguments: dict[str, Any]) -> str | None:
    coords = _coords(arguments)
    if coords is None:
        return None
    x, y = coords
    store = persistent._RUN_STORES.get(id(metrics))
    if store is None:
        return None
    try:
        document = json.loads(store.plan_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    execution = document.get("execution") if isinstance(document, dict) else None
    steps = execution.get("steps") if isinstance(execution, dict) else None
    if not isinstance(steps, list):
        return None

    # Look at the newest successful action at this coordinate. A successful placement of
    # an additive entity establishes ownership; a later successful mine removes it. Rotate
    # keeps ownership. This never grants permission over pre-existing factory entities.
    for step in reversed(steps):
        if not isinstance(step, dict) or step.get("failed") is True:
            continue
        step_args = step.get("arguments") if isinstance(step.get("arguments"), dict) else {}
        if not _same_point(step_args, x, y):
            continue
        tool = str(step.get("tool", ""))
        if tool == "mine_entity":
            return None
        if tool == "place_entity":
            entity_name = str(
                step_args.get("entityName")
                or step_args.get("entity_name")
                or step_args.get("name")
                or ""
            )
            return entity_name if entity_name in _OWNABLE_ENTITIES else None
        if tool == "rotate_entity":
            # Keep searching for the placement that established ownership.
            continue
        return None
    return None


async def _execute_function_calls_owned_additive(
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
        name = str(getattr(item, "name", ""))
        arguments = _arguments(item)
        owned_entity = None
        if plan_gated and metrics.plan_validated and name in {"mine_entity", "rotate_entity"}:
            owned_entity = _owned_additive_entity(metrics, arguments)

        if owned_entity is None:
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

        # Bypass only the exact-plan execution guard for this one proven AI-owned additive
        # entity. The lower persistence/resilience/mutation-budget stack still executes and
        # records the action normally.
        outputs.extend(
            await _LOW_LEVEL_EXECUTE_FUNCTION_CALLS(
                session,
                SimpleNamespace(output=[item]),
                settings,
                metrics,
                tool_names,
                plan_gated,
            )
        )
    return outputs


base._execute_function_calls = _execute_function_calls_owned_additive

base.SYSTEM_PROMPT += """

OWN-PLACEMENT CORRECTION:
You may mine or rotate an additive belt/underground-belt/electric-pole only when the runtime can prove that this AI placed that exact entity during the current persisted plan execution. This exists so you can correct your own routing mistakes. It never grants permission to rotate or remove pre-existing factory infrastructure.
"""
