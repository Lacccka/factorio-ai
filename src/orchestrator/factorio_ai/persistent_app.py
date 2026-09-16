from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from . import app as base
from .persistence import PersistentRunState
from .planning import PLAN_TOOL_NAME, PLAN_TOOL_SCHEMA


_ORIGINAL_EXECUTE_FUNCTION_CALLS = base._execute_function_calls
_ORIGINAL_ACTIVE_TOOLS = base._active_tools
_RUN_STORES: dict[int, PersistentRunState] = {}
MAX_FORCED_PLAN_CONTINUATIONS = 4
RESUME_DIAGNOSTIC_TOOL_BUDGET = 8
DEEPSEEK_HISTORY_MAX_GROUPS = 3
DEEPSEEK_HISTORY_MAX_CHARS = 120_000
PLAN_GATE_CONTINUE_PROMPT = """PLAN_GATE_INCOMPLETE: You attempted to finish this run before obtaining a fresh PLAN_VALID in the current process. Continue from the current plan/checkpoint instead of summarizing or stopping. If the stored/latest plan is PLAN_INVALID, fix only the remaining validator issues and call submit_factory_plan again. If a persisted checkpoint was PLAN_VALID, resubmit that exact stored plan once for fresh live validation before any mutation. Use narrow read-only checks only when needed; do not restart broad architecture discovery. Continue until fresh PLAN_VALID or until a genuine external/runtime blocker makes validation impossible."""


# When an exact-goal checkpoint already exists, the expensive architecture-discovery phase
# has already happened. Restrict the model to tools relevant to the remaining validator
# issue classes instead of letting a weaker/stateless model rediscover the whole factory.
_RESUME_COMMON_TOOLS = {
    "get_player_position",
    "get_nearby_entities",
    "get_area_occupancy",
    "inspect_entity",
    "inspect_entity_multiple",
    "check_entity_placement_batch",
    "get_entity_prototype",
}
_RESUME_POWER_TOOLS = {
    "get_power_network_topology",
    "get_electric_network",
    "plan_power_poles",
    "find_nearest_power_pole",
}
_RESUME_BELT_TOOLS = {
    "trace_item_flow",
    "get_flow_graph",
    "survey_factory_layout",
}
_RESUME_RATE_TOOLS = {
    "get_recipe_details",
    "calculate_production_rate",
    "get_item_fuel_info",
}
_RESUME_PLACEMENT_TOOLS = {
    "find_buildable_area",
}


def _arguments(item: Any) -> dict[str, Any]:
    try:
        value = json.loads(getattr(item, "arguments", "") or "{}")
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _store_for(settings: base.Settings, goal: str, metrics: base.RunMetrics) -> PersistentRunState:
    key = id(metrics)
    store = _RUN_STORES.get(key)
    if store is None:
        store = PersistentRunState(Path("state"), settings.player_name, goal)
        _RUN_STORES[key] = store
    return store


def _checkpoint_for_run(store: PersistentRunState, metrics: base.RunMetrics, plan_gated: bool) -> dict[str, Any] | None:
    if not plan_gated:
        return None
    _, checkpoint = store.plan_checkpoint_context()
    if not isinstance(checkpoint, dict):
        return None

    try:
        previous_attempt = int(checkpoint.get("validation_attempt") or 0)
    except (TypeError, ValueError):
        previous_attempt = 0
    if previous_attempt > metrics.plan_validation_attempts:
        metrics.plan_validation_attempts = previous_attempt

    # This is only informational state. A persisted PLAN_VALID never unlocks mutations;
    # the current process must submit it again and pass fresh live validation.
    setattr(metrics, "_latest_plan_validation", checkpoint)
    setattr(metrics, "_fresh_plan_validation_seen", False)
    setattr(metrics, "_resume_repair_mode", True)
    setattr(metrics, "_resume_diagnostic_calls", 0)
    return checkpoint


def _enrich_bootstrap(
    store: PersistentRunState,
    bootstrap: str,
    plan_gated: bool,
) -> str:
    sections = [bootstrap]
    factory_context, observation_count = store.factory_context()
    if factory_context:
        sections.append(factory_context)
        print(
            f"[persistence] loaded factory knowledge observations={observation_count}",
            file=sys.stderr,
        )

    if plan_gated:
        checkpoint_context, checkpoint = store.plan_checkpoint_context()
        if checkpoint_context:
            sections.append(checkpoint_context)
            print(
                "[persistence] loaded plan checkpoint "
                f"attempt={checkpoint.get('validation_attempt', '?')} "
                f"status={checkpoint.get('status', '?')} "
                f"issues={checkpoint.get('issue_count', '?')}",
                file=sys.stderr,
            )

    return "\n\n".join(section for section in sections if section)


def _resume_allowed_tool_names(validation: dict[str, Any] | None) -> set[str]:
    allowed = set(_RESUME_COMMON_TOOLS)
    if not isinstance(validation, dict):
        return allowed

    codes = {
        str(issue.get("code", ""))
        for issue in (validation.get("issues") or [])
        if isinstance(issue, dict)
    }
    if any("power" in code or "electric" in code for code in codes):
        allowed.update(_RESUME_POWER_TOOLS)
    if any(
        token in code
        for code in codes
        for token in ("belt", "route", "source", "sink", "direction", "continuity")
    ):
        allowed.update(_RESUME_BELT_TOOLS)
    if any(
        token in code
        for code in codes
        for token in ("rate", "throughput", "recipe", "fuel", "machine")
    ):
        allowed.update(_RESUME_RATE_TOOLS)
    if any(
        token in code
        for code in codes
        for token in ("place", "collision", "overlap", "inserter", "geometry")
    ):
        allowed.update(_RESUME_PLACEMENT_TOOLS)
    return allowed


def _active_tools_persistent(
    all_tools: list[dict[str, Any]],
    gated_tools: list[dict[str, Any]],
    read_only_tools: list[dict[str, Any]],
    metrics: base.RunMetrics,
    plan_gated: bool,
) -> list[dict[str, Any]]:
    tools = _ORIGINAL_ACTIVE_TOOLS(
        all_tools,
        gated_tools,
        read_only_tools,
        metrics,
        plan_gated,
    )
    if (
        not plan_gated
        or metrics.plan_validated
        or not bool(getattr(metrics, "_resume_repair_mode", False))
    ):
        return tools

    validation = getattr(metrics, "_latest_plan_validation", None)
    status = validation.get("status") if isinstance(validation, dict) else None
    fresh_seen = bool(getattr(metrics, "_fresh_plan_validation_seen", False))

    # A PLAN_VALID loaded from disk is not authorization to build. Do not let the model
    # spend tokens re-inspecting the factory: its only next planning action is a fresh
    # submit of the stored plan.
    if status == "PLAN_VALID" and not fresh_seen:
        return [PLAN_TOOL_SCHEMA]

    diagnostic_calls = int(getattr(metrics, "_resume_diagnostic_calls", 0) or 0)
    if diagnostic_calls >= RESUME_DIAGNOSTIC_TOOL_BUDGET:
        return [PLAN_TOOL_SCHEMA]

    allowed = _resume_allowed_tool_names(validation)
    filtered = [
        tool
        for tool in tools
        if str(tool.get("name", "")) == PLAN_TOOL_NAME
        or str(tool.get("name", "")) in allowed
    ]
    if not any(str(tool.get("name", "")) == PLAN_TOOL_NAME for tool in filtered):
        filtered.append(PLAN_TOOL_SCHEMA)
    return filtered


def _should_force_plan_continue(
    plan_gated: bool,
    metrics: base.RunMetrics,
    store: PersistentRunState,
    forced_count: int,
) -> bool:
    if not plan_gated or metrics.plan_validated or forced_count >= MAX_FORCED_PLAN_CONTINUATIONS:
        return False

    validation = getattr(metrics, "_latest_plan_validation", None)
    if not isinstance(validation, dict):
        _, validation = store.plan_checkpoint_context()
    if not isinstance(validation, dict):
        return False

    status = validation.get("status")
    fresh_seen = bool(getattr(metrics, "_fresh_plan_validation_seen", False))
    if status == "PLAN_VALID":
        # A PLAN_VALID loaded from disk is stale authorization. Force the model to submit it
        # once in this process; the base validator will set metrics.plan_validated only then.
        return not fresh_seen
    if status != "PLAN_INVALID":
        return False

    try:
        issue_count = int(validation.get("issue_count") or len(validation.get("issues") or []))
    except (TypeError, ValueError):
        issue_count = len(validation.get("issues") or [])
    return issue_count > 0


def _deepseek_history_input(
    initial_message: dict[str, Any],
    groups: list[list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Return bounded stateless history while preserving call/output groups atomically."""
    selected: list[list[dict[str, Any]]] = []
    used = 0
    for group in reversed(groups):
        rendered = json.dumps(group, ensure_ascii=False, separators=(",", ":"))
        group_chars = len(rendered)
        if selected and (
            len(selected) >= DEEPSEEK_HISTORY_MAX_GROUPS
            or used + group_chars > DEEPSEEK_HISTORY_MAX_CHARS
        ):
            break
        selected.append(group)
        used += group_chars
        if len(selected) >= DEEPSEEK_HISTORY_MAX_GROUPS or used >= DEEPSEEK_HISTORY_MAX_CHARS:
            break
    selected.reverse()
    result = [initial_message]
    for group in selected:
        result.extend(group)
    return result


async def _execute_function_calls_persistent(
    session: Any,
    response: Any,
    settings: base.Settings,
    metrics: base.RunMetrics,
    tool_names: set[str],
    plan_gated: bool,
) -> list[dict[str, Any]]:
    outputs = await _ORIGINAL_EXECUTE_FUNCTION_CALLS(
        session,
        response,
        settings,
        metrics,
        tool_names,
        plan_gated,
    )
    store = _RUN_STORES.get(id(metrics))
    if store is None:
        return outputs

    calls_by_id = {
        getattr(item, "call_id", ""): item
        for item in getattr(response, "output", []) or []
        if getattr(item, "type", None) == "function_call"
    }
    for output_item in outputs:
        call_id = str(output_item.get("call_id", ""))
        call = calls_by_id.get(call_id)
        if call is None:
            continue
        name = str(getattr(call, "name", ""))
        arguments = _arguments(call)
        tool_output = str(output_item.get("output", ""))

        if name == PLAN_TOOL_NAME:
            try:
                validation = json.loads(tool_output)
            except Exception:
                validation = None
            if isinstance(validation, dict) and validation.get("status") in {"PLAN_VALID", "PLAN_INVALID"}:
                setattr(metrics, "_latest_plan_validation", validation)
                setattr(metrics, "_fresh_plan_validation_seen", True)
                setattr(metrics, "_resume_diagnostic_calls", 0)
                store.save_plan_checkpoint(
                    metrics.plan_validation_attempts,
                    arguments.get("plan"),
                    validation,
                )
            continue

        mutating = base._is_mutating_tool(name)
        if not mutating:
            if (
                plan_gated
                and not metrics.plan_validated
                and bool(getattr(metrics, "_resume_repair_mode", False))
            ):
                current = int(getattr(metrics, "_resume_diagnostic_calls", 0) or 0)
                setattr(metrics, "_resume_diagnostic_calls", current + 1)
            store.record_observation(name, arguments, tool_output)
            continue

        blocked = tool_output.lstrip().startswith("MUTATION_BUDGET_REACHED:")
        failed = blocked or base._is_tool_failure(tool_output)
        if not failed:
            store.mark_world_mutation(name, arguments)
        if plan_gated and metrics.plan_validated:
            store.record_plan_execution_step(name, arguments, tool_output, failed)

    return outputs


async def _run_openai_stateful_persistent(
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
    store = _store_for(settings, goal, metrics)
    _checkpoint_for_run(store, metrics, plan_gated)
    enriched_bootstrap = _enrich_bootstrap(store, bootstrap, plan_gated)
    initial_input = f"USER GOAL:\n{goal}\n\nEXISTING SAVE BOOTSTRAP:\n{enriched_bootstrap}"
    response = await client.responses.create(
        model=settings.model,
        instructions=base.SYSTEM_PROMPT,
        input=initial_input,
        tools=base._active_tools(all_tools, gated_tools, read_only_tools, metrics, plan_gated),
    )
    base._record_response_usage(response, metrics)
    forced_count = 0

    for _ in range(settings.max_turns):
        outputs = await base._execute_function_calls(session, response, settings, metrics, tool_names, plan_gated)
        if not outputs:
            if _should_force_plan_continue(plan_gated, metrics, store, forced_count):
                forced_count += 1
                print(
                    "[plan-gate] rejected premature model completion; "
                    f"forcing continuation {forced_count}/{MAX_FORCED_PLAN_CONTINUATIONS}",
                    file=sys.stderr,
                )
                response = await client.responses.create(
                    model=settings.model,
                    instructions=base.SYSTEM_PROMPT,
                    previous_response_id=response.id,
                    input=PLAN_GATE_CONTINUE_PROMPT,
                    tools=base._active_tools(all_tools, gated_tools, read_only_tools, metrics, plan_gated),
                )
                base._record_response_usage(response, metrics)
                continue

            final_text = response.output_text or "(model completed without text output)"
            store.mark_run_finished(final_text)
            return final_text

        response = await client.responses.create(
            model=settings.model,
            instructions=base.SYSTEM_PROMPT,
            previous_response_id=response.id,
            input=outputs,
            tools=base._active_tools(all_tools, gated_tools, read_only_tools, metrics, plan_gated),
        )
        base._record_response_usage(response, metrics)

    raise RuntimeError(f"Agent exceeded AGENT_MAX_TURNS={settings.max_turns}.")


async def _run_deepseek_stateless_persistent(
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
    store = _store_for(settings, goal, metrics)
    _checkpoint_for_run(store, metrics, plan_gated)
    enriched_bootstrap = _enrich_bootstrap(store, bootstrap, plan_gated)
    initial_message: dict[str, Any] = {
        "role": "user",
        "content": f"USER GOAL:\n{goal}\n\nEXISTING SAVE BOOTSTRAP:\n{enriched_bootstrap}",
    }
    groups: list[list[dict[str, Any]]] = []
    forced_count = 0

    for _ in range(settings.max_turns):
        response = await client.responses.create(
            model=settings.model,
            instructions=base.SYSTEM_PROMPT,
            input=_deepseek_history_input(initial_message, groups),
            tools=base._active_tools(all_tools, gated_tools, read_only_tools, metrics, plan_gated),
        )
        base._record_response_usage(response, metrics)

        outputs = await base._execute_function_calls(session, response, settings, metrics, tool_names, plan_gated)
        assistant_items = [
            base._dump_model(item)
            for item in response.output
            if getattr(item, "type", None) in {"message", "function_call"}
        ]

        if not outputs:
            if _should_force_plan_continue(plan_gated, metrics, store, forced_count):
                forced_count += 1
                print(
                    "[plan-gate] rejected premature model completion; "
                    f"forcing continuation {forced_count}/{MAX_FORCED_PLAN_CONTINUATIONS}",
                    file=sys.stderr,
                )
                groups.append(
                    [
                        *[item for item in assistant_items if item.get("type") == "message"],
                        {"role": "user", "content": PLAN_GATE_CONTINUE_PROMPT},
                    ]
                )
                continue

            final_text = response.output_text or "(model completed without text output)"
            store.mark_run_finished(final_text)
            return final_text

        groups.append([*assistant_items, *outputs])

    raise RuntimeError(f"Agent exceeded AGENT_MAX_TURNS={settings.max_turns}.")


# Patch only the orchestration seams; the original app remains the single implementation
# of CLI parsing, MCP connection, budgets, tool execution and cleanup.
base._active_tools = _active_tools_persistent
base._execute_function_calls = _execute_function_calls_persistent
base._run_openai_stateful = _run_openai_stateful_persistent
base._run_deepseek_stateless = _run_deepseek_stateless_persistent


def main() -> None:
    base.main()


if __name__ == "__main__":
    main()
