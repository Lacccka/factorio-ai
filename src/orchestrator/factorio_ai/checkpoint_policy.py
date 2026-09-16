from __future__ import annotations

import json
from typing import Any

from . import persistence
from . import persistent_app as persistent


_ORIGINAL_PLAN_CHECKPOINT_CONTEXT = persistence.PersistentRunState.plan_checkpoint_context
_ORIGINAL_RECORD_PLAN_EXECUTION_STEP = persistence.PersistentRunState.record_plan_execution_step


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _ownership_key(arguments: dict[str, Any]) -> str | None:
    x = _number(arguments.get("x", arguments.get("targetX")))
    y = _number(arguments.get("y", arguments.get("targetY")))
    if x is None or y is None:
        return None
    return f"{x:.3f},{y:.3f}"


def _record_plan_execution_step_with_ownership(
    self: persistence.PersistentRunState,
    tool_name: str,
    arguments: dict[str, Any],
    output: str,
    failed: bool,
) -> None:
    _ORIGINAL_RECORD_PLAN_EXECUTION_STEP(self, tool_name, arguments, output, failed)
    if failed:
        return
    key = _ownership_key(arguments)
    if key is None or tool_name not in {"place_entity", "mine_entity", "rotate_entity"}:
        return

    document = persistence._load_json(self.plan_path)
    if document.get("player_name") != self.player_name or document.get("goal_hash") != self.goal_hash:
        return

    # Keep this ledger at the document top level. plan_checkpoint_context intentionally
    # does not expose it to the model, so hundreds of belt placements do not bloat prompt
    # context. It is purely a local authorization record for safe undo/rotate operations.
    owned = document.get("owned_additive")
    if not isinstance(owned, dict):
        owned = {}

    if tool_name == "place_entity":
        entity_name = str(
            arguments.get("entityName")
            or arguments.get("entity_name")
            or arguments.get("name")
            or ""
        )
        if entity_name:
            owned[key] = {"entity_name": entity_name, "x": arguments.get("x"), "y": arguments.get("y")}
    elif tool_name == "mine_entity":
        owned.pop(key, None)
    # rotate_entity keeps ownership unchanged.

    document["owned_additive"] = owned
    persistence._write_json(self.plan_path, document)


def _step_summary(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    # Tool output is deliberately excluded. In particular, an old
    # MUTATION_BUDGET_REACHED notice is process-local state and must never become a
    # persistent blocker on a later invocation.
    return {
        key: value.get(key)
        for key in ("at", "tool", "arguments", "failed")
        if key in value
    }


def _sanitize_checkpoint(compact: dict[str, Any]) -> dict[str, Any]:
    clean = dict(compact)
    clean.pop("last_run_final_text", None)

    execution = clean.get("execution")
    if isinstance(execution, dict):
        execution = dict(execution)
        historical_count = execution.pop("mutation_count", None)
        if historical_count is not None:
            execution["historical_mutation_count"] = historical_count

        last_step = _step_summary(execution.get("last_step"))
        if last_step is None:
            execution.pop("last_step", None)
        else:
            execution["last_step"] = last_step

        steps = execution.get("steps")
        if isinstance(steps, list):
            summaries = [summary for summary in (_step_summary(step) for step in steps[-8:]) if summary]
            execution["steps"] = summaries
        else:
            execution.pop("steps", None)
        clean["execution"] = execution

    return clean


def _plan_checkpoint_context_adaptive(
    self: persistence.PersistentRunState,
) -> tuple[str, dict[str, Any] | None]:
    _text, compact = _ORIGINAL_PLAN_CHECKPOINT_CONTEXT(self)
    if not isinstance(compact, dict):
        return "", None

    clean = _sanitize_checkpoint(compact)
    rendered = json.dumps(clean, ensure_ascii=False, separators=(",", ":"))
    text = (
        "ACTIVE PLAN CHECKPOINT FROM AN EARLIER RUN OF THIS EXACT GOAL:\n"
        "Continue from this architecture instead of restarting discovery. The orchestrator performs fresh local checkpoint "
        "validation before the cloud execution loop. If startup reports that the persisted plan is still PLAN_VALID and mutating "
        "tools are unlocked, continue execution immediately and do not submit the unchanged plan again. If the checkpoint remains "
        "PLAN_INVALID, repair only the listed architectural issues and resubmit. When execution.started=true, preserve useful prior "
        "work but do not assume every partial additive route is good merely because it was successfully placed. Stored ordinary "
        "belt/underground/pole coordinates are execution waypoints, not immutable commands; adapt those paths to the live geometry, "
        "and safely correct AI-owned additive placements that lead into the wrong network.\n"
        "IMPORTANT RUN-LIFETIME SEMANTICS: turn, mutation and failed-mutation budgets belong only to the current process invocation. "
        "A previous run ending with MUTATION_BUDGET_REACHED is historical information, not a current blocker. The current startup "
        "[limits]/[execution-budget] lines, current metrics and currently exposed tools are authoritative. historical_mutation_count "
        "below describes prior progress only and must never be compared with the current mutation limit. Do not stop merely because "
        "an earlier invocation exhausted its budget. Do not broadly resurvey unchanged areas.\n"
        + rendered
    )
    return text, clean


persistence.PersistentRunState.record_plan_execution_step = _record_plan_execution_step_with_ownership
persistence.PersistentRunState.plan_checkpoint_context = _plan_checkpoint_context_adaptive

# Keep forced continuation consistent with the same semantics. A locally revalidated
# PLAN_VALID already sets metrics.plan_validated=True, so this text is only relevant when
# planning genuinely remains incomplete.
persistent.PLAN_GATE_CONTINUE_PROMPT = """PLAN_GATE_INCOMPLETE: the current process does not yet have PLAN_VALID. Continue from the stored architecture instead of restarting discovery. Fix only the remaining validator issues and submit the corrected plan. If startup has already reported that the persisted plan is still PLAN_VALID and mutations are unlocked, do not resubmit it; continue execution instead. Stop only for a genuine current-process external/runtime blocker. A mutation/turn budget exhausted by an earlier invocation is not a blocker in this invocation."""
