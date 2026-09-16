from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from . import app as base
from .persistence import PersistentRunState
from .planning import PLAN_TOOL_NAME


_ORIGINAL_EXECUTE_FUNCTION_CALLS = base._execute_function_calls
_RUN_STORES: dict[int, PersistentRunState] = {}
MAX_FORCED_PLAN_CONTINUATIONS = 4
PLAN_GATE_CONTINUE_PROMPT = """PLAN_GATE_INCOMPLETE: You attempted to finish this run while the latest structured factory plan is still PLAN_INVALID. Do not summarize or stop yet. Continue from the current plan/checkpoint, fix the remaining validator issues, and call submit_factory_plan again. Use narrow read-only checks only when needed for the listed issues; do not restart broad architecture discovery. Continue until PLAN_VALID or until a genuine external/runtime blocker makes validation impossible."""


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
    if not isinstance(validation, dict) or validation.get("status") != "PLAN_INVALID":
        return False

    try:
        issue_count = int(validation.get("issue_count") or len(validation.get("issues") or []))
    except (TypeError, ValueError):
        issue_count = len(validation.get("issues") or [])
    return issue_count > 0


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
                store.save_plan_checkpoint(
                    metrics.plan_validation_attempts,
                    arguments.get("plan"),
                    validation,
                )
            continue

        mutating = base._is_mutating_tool(name)
        if not mutating:
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
    history: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": f"USER GOAL:\n{goal}\n\nEXISTING SAVE BOOTSTRAP:\n{enriched_bootstrap}",
        }
    ]
    forced_count = 0

    for _ in range(settings.max_turns):
        response = await client.responses.create(
            model=settings.model,
            instructions=base.SYSTEM_PROMPT,
            input=history,
            tools=base._active_tools(all_tools, gated_tools, read_only_tools, metrics, plan_gated),
        )
        base._record_response_usage(response, metrics)

        outputs = await base._execute_function_calls(session, response, settings, metrics, tool_names, plan_gated)
        if not outputs:
            if _should_force_plan_continue(plan_gated, metrics, store, forced_count):
                forced_count += 1
                print(
                    "[plan-gate] rejected premature model completion; "
                    f"forcing continuation {forced_count}/{MAX_FORCED_PLAN_CONTINUATIONS}",
                    file=sys.stderr,
                )
                for item in response.output:
                    if getattr(item, "type", None) == "message":
                        history.append(base._dump_model(item))
                history.append({"role": "user", "content": PLAN_GATE_CONTINUE_PROMPT})
                continue

            final_text = response.output_text or "(model completed without text output)"
            store.mark_run_finished(final_text)
            return final_text

        # DeepSeek Responses is stateless. Preserve assistant messages and function-call
        # items explicitly, then append the matching function_call_output items.
        for item in response.output:
            if getattr(item, "type", None) in {"message", "function_call"}:
                history.append(base._dump_model(item))
        history.extend(outputs)

    raise RuntimeError(f"Agent exceeded AGENT_MAX_TURNS={settings.max_turns}.")


# Patch only the orchestration seams; the original app remains the single implementation
# of CLI parsing, MCP connection, budgets, tool execution and cleanup.
base._execute_function_calls = _execute_function_calls_persistent
base._run_openai_stateful = _run_openai_stateful_persistent
base._run_deepseek_stateless = _run_deepseek_stateless_persistent


def main() -> None:
    base.main()


if __name__ == "__main__":
    main()
