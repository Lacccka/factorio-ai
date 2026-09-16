from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace
from typing import Any

from . import app as base
from . import plan_execution_guard as guard


_PRIOR_EXECUTE_FUNCTION_CALLS = base._execute_function_calls
_LOW_LEVEL_EXECUTE_FUNCTION_CALLS = guard._PRIOR_EXECUTE_FUNCTION_CALLS
_PROMPT_MARKER = "EXACT PLAN BATCH EXECUTION POLICY"
_DEFAULT_EXTRA_TURNS = 40


def _arguments(item: Any) -> dict[str, Any]:
    try:
        value = json.loads(getattr(item, "arguments", "") or "{}")
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _decode_targets(arguments: dict[str, Any]) -> list[dict[str, Any]] | None:
    raw: Any = None
    for key in ("targets", "placements", "placementsJson"):
        if key in arguments:
            raw = arguments[key]
            break
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return None
    if not isinstance(raw, list) or not raw:
        return None
    targets = [item for item in raw if isinstance(item, dict)]
    return targets if len(targets) == len(raw) else None


def _normalize_place_target(target: dict[str, Any]) -> dict[str, Any]:
    return {
        "entityName": target.get("entityName") or target.get("entity_name") or target.get("name"),
        "x": target.get("x"),
        "y": target.get("y"),
        "direction": target.get("direction", "north"),
    }


def _batch_authorized(plan: dict[str, Any], arguments: dict[str, Any]) -> tuple[bool, str]:
    targets = _decode_targets(arguments)
    if not targets:
        return False, "place_entity_multiple must contain a non-empty decodable target list"
    if len(targets) > 64:
        return False, "place_entity_multiple is limited to 64 exact planned targets per call"
    for index, target in enumerate(targets):
        normalized = _normalize_place_target(target)
        allowed, reason = guard._mutation_authorized("place_entity", normalized, plan)
        if not allowed:
            return False, f"target {index} is not plan-authorized: {reason}"
    return True, ""


def _extend_execution_turn_budget(settings: base.Settings, metrics: base.RunMetrics) -> None:
    if not metrics.plan_validated or bool(getattr(metrics, "_execution_turn_budget_extended", False)):
        return
    raw = os.getenv("AGENT_EXECUTION_EXTRA_TURNS", str(_DEFAULT_EXTRA_TURNS)).strip()
    try:
        extra = max(0, int(raw))
    except (TypeError, ValueError):
        extra = _DEFAULT_EXTRA_TURNS
    if extra:
        settings.max_turns += extra
        print(
            f"[execution-budget] added exact-plan execution turns={extra}; max_turns={settings.max_turns}",
            file=sys.stderr,
        )
    setattr(metrics, "_execution_turn_budget_extended", True)


async def _execute_function_calls_exact_batch(
    session: Any,
    response: Any,
    settings: base.Settings,
    metrics: base.RunMetrics,
    tool_names: set[str],
    plan_gated: bool,
) -> list[dict[str, Any]]:
    # A resumed PLAN_VALID may be established before the first model response.
    _extend_execution_turn_budget(settings, metrics)

    items = [
        item
        for item in getattr(response, "output", []) or []
        if getattr(item, "type", None) == "function_call"
    ]
    if not any(str(getattr(item, "name", "")) == "place_entity_multiple" for item in items):
        outputs = await _PRIOR_EXECUTE_FUNCTION_CALLS(
            session,
            response,
            settings,
            metrics,
            tool_names,
            plan_gated,
        )
        _extend_execution_turn_budget(settings, metrics)
        return outputs

    outputs: list[dict[str, Any]] = []
    for item in items:
        name = str(getattr(item, "name", ""))
        single_response = SimpleNamespace(output=[item])
        if (
            name != "place_entity_multiple"
            or not plan_gated
            or not metrics.plan_validated
            or not isinstance(metrics.validated_plan, dict)
        ):
            outputs.extend(
                await _PRIOR_EXECUTE_FUNCTION_CALLS(
                    session,
                    single_response,
                    settings,
                    metrics,
                    tool_names,
                    plan_gated,
                )
            )
            _extend_execution_turn_budget(settings, metrics)
            continue

        arguments = _arguments(item)
        allowed, reason = _batch_authorized(metrics.validated_plan, arguments)
        if not allowed:
            metrics.tool_calls += 1
            metrics.blocked_mutations += 1
            tool_output = (
                "PLAN_EXECUTION_BLOCKED: "
                + reason
                + ". Batch placement is only allowed when every target exactly belongs to the validated plan."
            )
            print(f"[plan-exec] blocked place_entity_multiple: {reason}", file=sys.stderr)
            outputs.append(
                {
                    "type": "function_call_output",
                    "call_id": item.call_id,
                    "output": tool_output,
                }
            )
            continue

        # Additive exact-plan placement is safe to batch even if FactorioMCP reports a
        # partial failure: every successful target is authorized and idempotent resume can
        # reconcile it. Destructive *_multiple operations remain blocked by the inner guard.
        outputs.extend(
            await _LOW_LEVEL_EXECUTE_FUNCTION_CALLS(
                session,
                single_response,
                settings,
                metrics,
                tool_names,
                plan_gated,
            )
        )
    return outputs


base._execute_function_calls = _execute_function_calls_exact_batch

if _PROMPT_MARKER not in base.SYSTEM_PROMPT:
    base.SYSTEM_PROMPT += """

EXACT PLAN BATCH EXECUTION POLICY:
After PLAN_VALID, execution is mechanical. Do not repeatedly inspect each successful placement. If place_entity_multiple is available, use it for small spatially local batches of exact validated placements or ordinary route-belt tiles; every target must already belong to the validated plan. Destructive batch mutations such as mine_entity_multiple remain forbidden. For out_of_range, walk near the next local cluster and retry the same planned work. Reserve inspections for genuine failures and the final verification phase.
"""
