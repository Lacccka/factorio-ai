from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from typing import Any

from . import app as base


_PRIOR_EXECUTE_FUNCTION_CALLS = base._execute_function_calls
_PROMPT_MARKER = "SEQUENTIAL LIVE EXECUTION"


def _arguments(item: Any) -> dict[str, Any]:
    try:
        value = json.loads(getattr(item, "arguments", "") or "{}")
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _payload(output: str) -> dict[str, Any] | None:
    text = output.split("\n\nMUTATION_BUDGET_REACHED:", 1)[0].strip()
    try:
        value = json.loads(text)
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def _output_text(output_item: dict[str, Any]) -> str:
    return str(output_item.get("output", ""))


def _is_out_of_range(outputs: list[dict[str, Any]]) -> bool:
    if len(outputs) != 1:
        return False
    payload = _payload(_output_text(outputs[0]))
    return isinstance(payload, dict) and str(payload.get("error", "")).lower() == "out_of_range"


def _mutation_failed(outputs: list[dict[str, Any]]) -> bool:
    if not outputs:
        return True
    hard_prefixes = (
        "PLAN_EXECUTION_BLOCKED:",
        "MUTATION_BUDGET_REACHED:",
        "PLACEMENT_RANGE_RECOVERY_FAILED:",
        "EXECUTION_DEFERRED:",
    )
    for item in outputs:
        text = _output_text(item).lstrip()
        if text.startswith(hard_prefixes) or base._is_tool_failure(text):
            return True
    return False


def _range_target(arguments: dict[str, Any]) -> tuple[float, float] | None:
    try:
        x = float(arguments.get("x"))
        y = float(arguments.get("y"))
    except (TypeError, ValueError):
        return None
    return x, y


def _refund_range_miss(settings: base.Settings, metrics: base.RunMetrics) -> None:
    # out_of_range changes no world state. It is a movement miss, not a consumed safety
    # mutation. The optimized runtime already excludes it from failed_mutations; exclude it
    # from the successful-mutation allowance as well.
    if metrics.mutation_calls > 0:
        metrics.mutation_calls -= 1
    if (
        metrics.mutation_budget_exhausted
        and metrics.mutation_calls < settings.max_mutations
        and metrics.failed_mutations < settings.max_failed_mutations
    ):
        metrics.mutation_budget_exhausted = False


async def _walk_near(
    session: Any,
    target: tuple[float, float],
    settings: base.Settings,
    metrics: base.RunMetrics,
    tool_names: set[str],
) -> str | None:
    if "walk_to_position" not in tool_names:
        return None
    metrics.tool_calls += 1
    x, y = target
    output = await base._call_tool(
        session,
        "walk_to_position",
        {"targetX": x, "targetY": y, "tolerance": 5, "timeoutSeconds": 30},
        settings.tool_result_max_chars,
    )
    print(f"[execution-walk] target=({x},{y}) -> {output[:240]}", file=sys.stderr)
    payload = _payload(output)
    if not isinstance(payload, dict) or str(payload.get("status", "")).lower() != "arrived":
        return output
    return ""


async def _execute_function_calls_sequentially(
    session: Any,
    response: Any,
    settings: base.Settings,
    metrics: base.RunMetrics,
    tool_names: set[str],
    plan_gated: bool,
) -> list[dict[str, Any]]:
    outputs: list[dict[str, Any]] = []
    stop_dependent_mutations = False
    stop_reason = ""

    for item in getattr(response, "output", []) or []:
        if getattr(item, "type", None) != "function_call":
            continue
        name = str(getattr(item, "name", ""))
        mutating = base._is_mutating_tool(name)

        if mutating and stop_dependent_mutations:
            metrics.tool_calls += 1
            metrics.blocked_mutations += 1
            outputs.append(
                {
                    "type": "function_call_output",
                    "call_id": item.call_id,
                    "output": (
                        "EXECUTION_DEFERRED: an earlier mutation in this same model response failed in the live world. "
                        f"Remaining dependent mutations were not executed ({stop_reason}). Observe/adapt first, then issue only the still-needed work."
                    ),
                }
            )
            continue

        single = SimpleNamespace(output=[item])
        current = await _PRIOR_EXECUTE_FUNCTION_CALLS(
            session,
            single,
            settings,
            metrics,
            tool_names,
            plan_gated,
        )

        if mutating and name == "place_entity" and _is_out_of_range(current):
            _refund_range_miss(settings, metrics)
            target = _range_target(_arguments(item))
            walk_failure = None if target is None else await _walk_near(
                session,
                target,
                settings,
                metrics,
                tool_names,
            )
            if target is not None and walk_failure == "":
                current = await _PRIOR_EXECUTE_FUNCTION_CALLS(
                    session,
                    single,
                    settings,
                    metrics,
                    tool_names,
                    plan_gated,
                )
            elif walk_failure:
                current = [
                    {
                        "type": "function_call_output",
                        "call_id": item.call_id,
                        "output": (
                            "PLACEMENT_RANGE_RECOVERY_FAILED: automatic movement to the model-selected placement target failed. "
                            f"movement={walk_failure}"
                        ),
                    }
                ]

        outputs.extend(current)

        if mutating and _mutation_failed(current):
            # A collision/invalid target is new live-world information. Do not continue a
            # pre-generated chain of placements past it: that creates disconnected belts
            # and spends budget before Sol can see the obstacle.
            stop_dependent_mutations = True
            payload = _payload(_output_text(current[0])) if current else None
            if isinstance(payload, dict):
                stop_reason = str(payload.get("error") or payload.get("status") or "mutation failure")
            else:
                first_text = _output_text(current[0]).strip() if current else ""
                stop_reason = first_text.split(":", 1)[0] or "mutation failure"

    return outputs


base._execute_function_calls = _execute_function_calls_sequentially

if _PROMPT_MARKER not in base.SYSTEM_PROMPT:
    base.SYSTEM_PROMPT += """

SEQUENTIAL LIVE EXECUTION:
You may request several nearby additive placements in one turn, but treat them as a hypothesis about the live geometry. The runtime automatically handles pure out_of_range placement misses by walking near the exact target and retrying. If a real placement collision, policy failure, movement failure, or other mutation failure occurs, later mutations from that same response are deferred so you can observe the obstacle and reason before building past it. Do not pre-generate a second disconnected route beyond a failed tile.
"""
