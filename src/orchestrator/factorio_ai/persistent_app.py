from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from . import app as base
from .persistence import PersistentRunState
from .planning import PLAN_TOOL_NAME


_ORIGINAL_EXECUTE_FUNCTION_CALLS = base._execute_function_calls
_ORIGINAL_RUN_OPENAI_STATEFUL = base._run_openai_stateful
_ORIGINAL_RUN_DEEPSEEK_STATELESS = base._run_deepseek_stateless
_RUN_STORES: dict[int, PersistentRunState] = {}


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
    enriched_bootstrap = _enrich_bootstrap(store, bootstrap, plan_gated)
    final_text = await _ORIGINAL_RUN_OPENAI_STATEFUL(
        session,
        client,
        settings,
        goal,
        all_tools,
        gated_tools,
        read_only_tools,
        enriched_bootstrap,
        metrics,
        tool_names,
        plan_gated,
    )
    store.mark_run_finished(final_text)
    return final_text


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
    enriched_bootstrap = _enrich_bootstrap(store, bootstrap, plan_gated)
    final_text = await _ORIGINAL_RUN_DEEPSEEK_STATELESS(
        session,
        client,
        settings,
        goal,
        all_tools,
        gated_tools,
        read_only_tools,
        enriched_bootstrap,
        metrics,
        tool_names,
        plan_gated,
    )
    store.mark_run_finished(final_text)
    return final_text


# Patch only the orchestration seams; the original app remains the single implementation
# of CLI parsing, MCP connection, budgets, provider behavior and cleanup.
base._execute_function_calls = _execute_function_calls_persistent
base._run_openai_stateful = _run_openai_stateful_persistent
base._run_deepseek_stateless = _run_deepseek_stateless_persistent


def main() -> None:
    base.main()


if __name__ == "__main__":
    main()
