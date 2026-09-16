from __future__ import annotations

import os
import sys
from typing import Any

from . import app as base


_PRIOR_EXECUTE_FUNCTION_CALLS = base._execute_function_calls
_DEFAULT_EXTRA_TURNS = 40


def _extend_execution_turn_budget(settings: base.Settings, metrics: base.RunMetrics) -> None:
    if not metrics.plan_validated or bool(getattr(metrics, "_execution_turn_budget_extended", False)):
        return
    raw = os.getenv("AGENT_EXECUTION_EXTRA_TURNS", str(_DEFAULT_EXTRA_TURNS)).strip()
    try:
        extra = max(0, int(raw))
    except (TypeError, ValueError):
        extra = _DEFAULT_EXTRA_TURNS
    if extra:
        # Settings is frozen by design; the running loop still needs a process-local
        # execution allowance after planning has succeeded.
        object.__setattr__(settings, "max_turns", int(settings.max_turns) + extra)
        print(
            f"[execution-budget] added execution turns={extra}; max_turns={settings.max_turns}",
            file=sys.stderr,
        )
    setattr(metrics, "_execution_turn_budget_extended", True)


async def _execute_function_calls_with_budget(
    session: Any,
    response: Any,
    settings: base.Settings,
    metrics: base.RunMetrics,
    tool_names: set[str],
    plan_gated: bool,
) -> list[dict[str, Any]]:
    # Resume may establish PLAN_VALID before the first cloud response.
    _extend_execution_turn_budget(settings, metrics)
    outputs = await _PRIOR_EXECUTE_FUNCTION_CALLS(
        session,
        response,
        settings,
        metrics,
        tool_names,
        plan_gated,
    )
    # A submit_factory_plan call may establish PLAN_VALID during this response.
    _extend_execution_turn_budget(settings, metrics)
    return outputs


base._execute_function_calls = _execute_function_calls_with_budget
