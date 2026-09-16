from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from typing import Any

from . import persistent_app as persistent
from . import app as base
from .plan_repair import repair_power_network_disconnect
from .planning import PLAN_TOOL_NAME, validate_factory_plan


# persistent_app has already patched these base seams by the time this module imports.
_PERSISTENT_EXECUTE_FUNCTION_CALLS = base._execute_function_calls
_PERSISTENT_RUN_OPENAI_STATEFUL = base._run_openai_stateful
_PERSISTENT_RUN_DEEPSEEK_STATELESS = base._run_deepseek_stateless
_ORIGINAL_RESUME_ALLOWED_TOOL_NAMES = persistent._resume_allowed_tool_names
_ORIGINAL_ENRICH_BOOTSTRAP = persistent._enrich_bootstrap

# A resumed plan is not a new planning job. Four local diagnostic calls are enough to
# repair a small validator issue; after that the model must resubmit instead of exploring.
persistent.RESUME_DIAGNOSTIC_TOOL_BUDGET = 4
persistent.DEEPSEEK_HISTORY_MAX_GROUPS = 2
persistent.DEEPSEEK_HISTORY_MAX_CHARS = 60_000


def _checkpoint_issue_codes(validation: Any) -> set[str]:
    if not isinstance(validation, dict):
        return set()
    return {
        str(item.get("code", ""))
        for item in (validation.get("issues") or [])
        if isinstance(item, dict) and str(item.get("code", ""))
    }


def _strict_resume_allowed_tool_names(validation: dict[str, Any] | None) -> set[str]:
    codes = _checkpoint_issue_codes(validation)
    if codes and codes <= {"power_network_disconnected"}:
        return {
            "get_power_network_topology",
            "get_electric_network",
            "get_nearby_entities",
            "get_area_occupancy",
            "check_entity_placement_batch",
            "plan_power_poles",
            "find_nearest_power_pole",
        }
    return _ORIGINAL_RESUME_ALLOWED_TOOL_NAMES(validation)


def _slim_resume_bootstrap(
    store: persistent.PersistentRunState,
    bootstrap: str,
    plan_gated: bool,
) -> str:
    if plan_gated:
        checkpoint_context, checkpoint = store.plan_checkpoint_context()
        if isinstance(checkpoint, dict) and checkpoint.get("status") == "PLAN_INVALID":
            print(
                "[persistence] loaded plan checkpoint "
                f"attempt={checkpoint.get('validation_attempt', '?')} "
                f"status={checkpoint.get('status', '?')} "
                f"issues={checkpoint.get('issue_count', '?')}",
                file=sys.stderr,
            )
            print(
                "[resume-gate] compact checkpoint context enabled; broad bootstrap/knowledge omitted",
                file=sys.stderr,
            )
            return (
                "RESUME REPAIR MODE. The earlier architecture-discovery phase is complete. "
                "Do not rediscover the factory. Work only on the stored validator issues below.\n\n"
                + checkpoint_context
            )
    return _ORIGINAL_ENRICH_BOOTSTRAP(store, bootstrap, plan_gated)


def _resume_call_allowed(metrics: base.RunMetrics, name: str) -> tuple[bool, str]:
    if not bool(getattr(metrics, "_resume_repair_mode", False)) or metrics.plan_validated:
        return True, ""
    if name == PLAN_TOOL_NAME:
        return True, ""

    validation = getattr(metrics, "_latest_plan_validation", None)
    allowed = _strict_resume_allowed_tool_names(validation if isinstance(validation, dict) else None)
    if name not in allowed:
        return False, "tool is unrelated to the remaining persisted validator issue"

    diagnostic_calls = int(getattr(metrics, "_resume_diagnostic_calls", 0) or 0)
    if diagnostic_calls >= persistent.RESUME_DIAGNOSTIC_TOOL_BUDGET:
        return False, "resume diagnostic budget exhausted; resubmit the stored plan now"
    return True, ""


async def _execute_function_calls_resilient(
    session: Any,
    response: Any,
    settings: base.Settings,
    metrics: base.RunMetrics,
    tool_names: set[str],
    plan_gated: bool,
) -> list[dict[str, Any]]:
    # Outside resume-repair mode retain the existing batched implementation.
    if (
        not plan_gated
        or metrics.plan_validated
        or not bool(getattr(metrics, "_resume_repair_mode", False))
    ):
        return await _PERSISTENT_EXECUTE_FUNCTION_CALLS(
            session,
            response,
            settings,
            metrics,
            tool_names,
            plan_gated,
        )

    # DeepSeek can emit dozens of function calls in a single response. Enforce the resume
    # policy per call, not merely by changing the next turn's tool schema.
    outputs: list[dict[str, Any]] = []
    for item in getattr(response, "output", []) or []:
        if getattr(item, "type", None) != "function_call":
            continue
        name = str(getattr(item, "name", ""))
        allowed, reason = _resume_call_allowed(metrics, name)
        if not allowed:
            metrics.tool_calls += 1
            tool_output = (
                "RESUME_TOOL_BLOCKED: "
                + reason
                + ". Do not replace this with another broad diagnostic. Correct the stored plan and call submit_factory_plan."
            )
            print(f"[resume-gate] blocked {name}: {reason}", file=sys.stderr)
            outputs.append(
                {
                    "type": "function_call_output",
                    "call_id": item.call_id,
                    "output": tool_output,
                }
            )
            continue

        single_response = SimpleNamespace(output=[item])
        executed = await _PERSISTENT_EXECUTE_FUNCTION_CALLS(
            session,
            single_response,
            settings,
            metrics,
            tool_names,
            plan_gated,
        )
        outputs.extend(executed)
    return outputs


async def _revalidate_and_repair_checkpoint(
    session: Any,
    settings: base.Settings,
    metrics: base.RunMetrics,
    tool_names: set[str],
    store: persistent.PersistentRunState,
    plan_gated: bool,
) -> None:
    if not plan_gated:
        return
    _, checkpoint = store.plan_checkpoint_context()
    if not isinstance(checkpoint, dict):
        return
    plan = checkpoint.get("plan")
    if not isinstance(plan, dict):
        return

    async def call_mcp(name: str, arguments: dict[str, Any]) -> str:
        return await base._call_tool(session, name, arguments, settings.tool_result_max_chars)

    metrics.plan_validation_attempts = max(
        metrics.plan_validation_attempts,
        int(checkpoint.get("validation_attempt") or 0),
    )
    metrics.plan_validation_attempts += 1
    validation = await validate_factory_plan(plan, call_mcp, tool_names)
    store.save_plan_checkpoint(metrics.plan_validation_attempts, plan, validation)
    setattr(metrics, "_latest_plan_validation", validation)
    setattr(metrics, "_fresh_plan_validation_seen", True)
    print(
        f"[resume-gate] local revalidation attempt={metrics.plan_validation_attempts} "
        f"status={validation.get('status')} issues={validation.get('issue_count', '?')}",
        file=sys.stderr,
    )

    if validation.get("valid") is True:
        metrics.plan_validated = True
        metrics.validated_plan = plan
        print("[resume-gate] persisted plan is still PLAN_VALID; mutating tools unlocked", file=sys.stderr)
        return

    repaired = await repair_power_network_disconnect(plan, validation, call_mcp)
    if repaired is None:
        return

    metrics.plan_validation_attempts += 1
    repaired_validation = await validate_factory_plan(repaired, call_mcp, tool_names)
    old_count = int(validation.get("issue_count") or 0)
    new_count = int(repaired_validation.get("issue_count") or 0)
    if repaired_validation.get("valid") is True or new_count < old_count:
        store.save_plan_checkpoint(metrics.plan_validation_attempts, repaired, repaired_validation)
        setattr(metrics, "_latest_plan_validation", repaired_validation)
        setattr(metrics, "_fresh_plan_validation_seen", True)
        print(
            f"[resume-gate] deterministic power repair status={repaired_validation.get('status')} "
            f"issues={new_count}",
            file=sys.stderr,
        )
        if repaired_validation.get("valid") is True:
            metrics.plan_validated = True
            metrics.validated_plan = repaired
            print("[resume-gate] deterministic repair reached PLAN_VALID; mutating tools unlocked", file=sys.stderr)


async def _run_openai_stateful_resilient(
    session: Any,
    client: Any,
    settings: base.Settings,
    goal: str,
    all_tools: list[dict[str, Any]],
    gated_tools: list[dict[str, Any]],
    read_only_tools: list[dict[str, Any]],
    bootstrap: str,
    metrics: base.RunMetrics,
    tool_names: set[str],
    plan_gated: bool,
) -> str:
    store = persistent._store_for(settings, goal, metrics)
    await _revalidate_and_repair_checkpoint(session, settings, metrics, tool_names, store, plan_gated)
    return await _PERSISTENT_RUN_OPENAI_STATEFUL(
        session,
        client,
        settings,
        goal,
        all_tools,
        gated_tools,
        read_only_tools,
        bootstrap,
        metrics,
        tool_names,
        plan_gated,
    )


async def _run_deepseek_stateless_resilient(
    session: Any,
    client: Any,
    settings: base.Settings,
    goal: str,
    all_tools: list[dict[str, Any]],
    gated_tools: list[dict[str, Any]],
    read_only_tools: list[dict[str, Any]],
    bootstrap: str,
    metrics: base.RunMetrics,
    tool_names: set[str],
    plan_gated: bool,
) -> str:
    store = persistent._store_for(settings, goal, metrics)
    await _revalidate_and_repair_checkpoint(session, settings, metrics, tool_names, store, plan_gated)
    return await _PERSISTENT_RUN_DEEPSEEK_STATELESS(
        session,
        client,
        settings,
        goal,
        all_tools,
        gated_tools,
        read_only_tools,
        bootstrap,
        metrics,
        tool_names,
        plan_gated,
    )


# Make the existing persistent implementation stricter without duplicating the full CLI.
persistent._resume_allowed_tool_names = _strict_resume_allowed_tool_names
persistent._enrich_bootstrap = _slim_resume_bootstrap
base._execute_function_calls = _execute_function_calls_resilient
base._run_openai_stateful = _run_openai_stateful_resilient
base._run_deepseek_stateless = _run_deepseek_stateless_resilient


def main() -> None:
    base.main()


if __name__ == "__main__":
    main()
