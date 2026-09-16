from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from typing import Any

from . import app as base
from . import persistent_app as persistent


_PRIOR_EXECUTE_FUNCTION_CALLS = base._execute_function_calls
_PROMPT_MARKER = "ADAPTIVE EXECUTION REPLAN POLICY"
_SPATIAL_ERRORS = {
    "invalid_position",
    "cannot_place",
    "cannot_place_entity",
    "collision",
    "blocked",
    "tile_blocked",
    "entity_collision",
}


def _arguments(item: Any) -> dict[str, Any]:
    try:
        value = json.loads(getattr(item, "arguments", "") or "{}")
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _parse_json(output: str) -> dict[str, Any] | None:
    text = output.split("\n\nMUTATION_BUDGET_REACHED:", 1)[0].strip()
    try:
        value = json.loads(text)
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def _find_spatial_error(value: Any) -> str | None:
    if isinstance(value, dict):
        error = str(value.get("error", "")).strip().lower()
        if error in _SPATIAL_ERRORS:
            return error
        status = str(value.get("status", "")).strip().lower()
        if status in _SPATIAL_ERRORS:
            return status
        for key in ("results", "failures", "errors"):
            nested = value.get(key)
            if isinstance(nested, list):
                for item in nested:
                    found = _find_spatial_error(item)
                    if found:
                        return found
            elif isinstance(nested, dict):
                found = _find_spatial_error(nested)
                if found:
                    return found
    elif isinstance(value, list):
        for item in value:
            found = _find_spatial_error(item)
            if found:
                return found
    return None


def _spatial_conflict(name: str, output: str) -> str | None:
    if name not in {"place_entity", "place_entity_multiple"}:
        return None
    payload = _parse_json(output)
    if payload is not None:
        return _find_spatial_error(payload)
    lowered = output.lower()
    if "out_of_range" in lowered:
        return None
    for error in _SPATIAL_ERRORS:
        if error in lowered:
            return error
    return None


def _enter_repair_mode(
    metrics: base.RunMetrics,
    tool_name: str,
    arguments: dict[str, Any],
    tool_output: str,
    error: str,
) -> dict[str, Any]:
    plan = metrics.validated_plan if isinstance(metrics.validated_plan, dict) else None
    entity_name = str(arguments.get("entityName") or arguments.get("entity_name") or "")
    x = arguments.get("x")
    y = arguments.get("y")
    issue_code = "execution_route_collision" if "belt" in entity_name else "execution_placement_collision"
    issue = {
        "code": issue_code,
        "message": (
            "A mutation that was authorized by PLAN_VALID is impossible in the live world. "
            "Suspend execution, preserve completed work, inspect only this local conflict, "
            "repair the affected route/placement, and resubmit the complete plan."
        ),
        "tool": tool_name,
        "error": error,
        "entity_name": entity_name or None,
        "x": x,
        "y": y,
        "arguments": arguments,
        "tool_output": tool_output[:2_000],
    }
    validation = {
        "valid": False,
        "status": "PLAN_INVALID",
        "issue_count": 1,
        "warning_count": 0,
        "issues": [issue],
        "warnings": [],
        "message": (
            "Live execution contradicted the validated world snapshot. Mutations are locked again "
            "until a locally repaired full plan receives PLAN_VALID."
        ),
    }

    # Revoke execution authorization immediately. Keep the old validated_plan in memory as
    # the repair baseline; base guards require plan_validated=True before allowing mutation.
    metrics.plan_validated = False
    setattr(metrics, "_latest_plan_validation", validation)
    setattr(metrics, "_fresh_plan_validation_seen", True)
    setattr(metrics, "_resume_repair_mode", True)
    setattr(metrics, "_resume_diagnostic_calls", 0)

    store = persistent._RUN_STORES.get(id(metrics))
    if store is not None and plan is not None:
        store.save_plan_checkpoint(metrics.plan_validation_attempts, plan, validation)

    print(
        f"[adaptive-replan] revoked PLAN_VALID after {tool_name} {error}; local repair required",
        file=sys.stderr,
    )
    return validation


async def _execute_function_calls_adaptive(
    session: Any,
    response: Any,
    settings: base.Settings,
    metrics: base.RunMetrics,
    tool_names: set[str],
    plan_gated: bool,
) -> list[dict[str, Any]]:
    if not plan_gated or not metrics.plan_validated:
        return await _PRIOR_EXECUTE_FUNCTION_CALLS(
            session,
            response,
            settings,
            metrics,
            tool_names,
            plan_gated,
        )

    outputs: list[dict[str, Any]] = []
    repair_active = False
    for item in getattr(response, "output", []) or []:
        if getattr(item, "type", None) != "function_call":
            continue
        name = str(getattr(item, "name", ""))

        if repair_active and base._is_mutating_tool(name):
            metrics.tool_calls += 1
            metrics.blocked_mutations += 1
            outputs.append(
                {
                    "type": "function_call_output",
                    "call_id": item.call_id,
                    "output": (
                        "ADAPTIVE_REPLAN_ACTIVE: a prior live collision invalidated PLAN_VALID. "
                        "This mutation was not executed. Inspect only the local conflict and resubmit a repaired plan."
                    ),
                }
            )
            continue

        single_response = SimpleNamespace(output=[item])
        one = await _PRIOR_EXECUTE_FUNCTION_CALLS(
            session,
            single_response,
            settings,
            metrics,
            tool_names,
            plan_gated,
        )
        outputs.extend(one)
        if not metrics.plan_validated:
            # An inner layer may already have revoked authorization.
            repair_active = True
            continue

        if not base._is_mutating_tool(name):
            continue
        matching = next(
            (
                output
                for output in one
                if isinstance(output, dict) and str(output.get("call_id", "")) == str(item.call_id)
            ),
            None,
        )
        if matching is None:
            continue
        tool_output = str(matching.get("output", ""))
        error = _spatial_conflict(name, tool_output)
        if not error:
            continue

        validation = _enter_repair_mode(metrics, name, _arguments(item), tool_output, error)
        matching["output"] = (
            tool_output
            + "\n\nADAPTIVE_REPLAN_REQUIRED: "
            + validation["message"]
            + " Preserve successful earlier placements; do not restart broad discovery."
        )
        repair_active = True

    return outputs


base._execute_function_calls = _execute_function_calls_adaptive

if _PROMPT_MARKER not in base.SYSTEM_PROMPT:
    base.SYSTEM_PROMPT += """

ADAPTIVE EXECUTION REPLAN POLICY:
PLAN_VALID validates a world snapshot; it is not a command to ignore contradictory live evidence. If an exact authorized placement fails because the live tile is blocked/colliding, execution authorization is revoked automatically and the run returns to narrow repair mode. Preserve all completed placements, inspect only the affected local corridor, modify the minimum necessary route/placement geometry, and resubmit the complete structured plan. After the repaired plan reaches PLAN_VALID, continue execution from the already-completed world state. Do not terminate the task merely because the previous plan became stale, and do not improvise mutations while that stale plan is invalid.
"""
