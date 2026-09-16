from __future__ import annotations

import os
import sys
from typing import Any

from . import app as base


_PRIOR_EXECUTE_FUNCTION_CALLS = base._execute_function_calls
_DEFAULT_EXTRA_TURNS = 40
_DEFAULT_EXTRA_MUTATIONS = 240


def _nonnegative_env(name: str, fallback: int) -> int:
    raw = os.getenv(name, str(fallback)).strip()
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return fallback


def _extend_execution_budget(settings: base.Settings, metrics: base.RunMetrics) -> None:
    if not metrics.plan_validated or bool(getattr(metrics, "_execution_budget_extended", False)):
        return

    extra_turns = _nonnegative_env("AGENT_EXECUTION_EXTRA_TURNS", _DEFAULT_EXTRA_TURNS)
    extra_mutations = _nonnegative_env("AGENT_EXECUTION_EXTRA_MUTATIONS", _DEFAULT_EXTRA_MUTATIONS)

    # Settings is frozen by design; these are process-local allowances for the already
    # validated execution phase. Structural/destructive safety is still enforced by the
    # plan guards, so a long belt build should not exhaust the same tiny budget intended
    # to limit unsafe exploratory mutation.
    if extra_turns:
        object.__setattr__(settings, "max_turns", int(settings.max_turns) + extra_turns)
    if extra_mutations:
        object.__setattr__(settings, "max_mutations", int(settings.max_mutations) + extra_mutations)

    print(
        "[execution-budget] "
        f"added turns={extra_turns} mutations={extra_mutations}; "
        f"max_turns={settings.max_turns} max_mutations={settings.max_mutations}",
        file=sys.stderr,
    )
    setattr(metrics, "_execution_budget_extended", True)
    # Backward-compatible marker for older tests/checkpoints that only knew about turns.
    setattr(metrics, "_execution_turn_budget_extended", True)


def _extend_execution_turn_budget(settings: base.Settings, metrics: base.RunMetrics) -> None:
    """Compatibility alias for older callers; execution now extends both allowances."""
    _extend_execution_budget(settings, metrics)


async def _execute_function_calls_with_budget(
    session: Any,
    response: Any,
    settings: base.Settings,
    metrics: base.RunMetrics,
    tool_names: set[str],
    plan_gated: bool,
) -> list[dict[str, Any]]:
    # Resume may establish PLAN_VALID before the first cloud response.
    _extend_execution_budget(settings, metrics)
    outputs = await _PRIOR_EXECUTE_FUNCTION_CALLS(
        session,
        response,
        settings,
        metrics,
        tool_names,
        plan_gated,
    )
    # A submit_factory_plan call may establish PLAN_VALID during this response.
    _extend_execution_budget(settings, metrics)
    return outputs


base._execute_function_calls = _execute_function_calls_with_budget
