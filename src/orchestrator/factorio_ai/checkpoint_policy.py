from __future__ import annotations

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
    execution = document.get("execution")
    if not isinstance(execution, dict):
        return
    owned = execution.get("owned_additive")
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

    execution["owned_additive"] = owned
    document["execution"] = execution
    persistence._write_json(self.plan_path, document)


def _plan_checkpoint_context_adaptive(
    self: persistence.PersistentRunState,
) -> tuple[str, dict[str, Any] | None]:
    text, compact = _ORIGINAL_PLAN_CHECKPOINT_CONTEXT(self)
    if not text:
        return text, compact

    old = (
        "If it is PLAN_VALID, resubmit the stored plan once for fresh live validation before any mutation; previous validation "
        "never unlocks mutations across process restarts. If execution.started=true, assume the prior run may have partially "
        "executed the plan and revalidate only the affected placements/state before continuing. Do not broadly resurvey unchanged areas."
    )
    new = (
        "The orchestrator performs fresh local checkpoint validation before the cloud execution loop. If startup reports that "
        "the persisted plan is still PLAN_VALID and mutating tools are unlocked, continue execution immediately and do not submit "
        "the unchanged plan again. If the checkpoint remains PLAN_INVALID, repair only the listed architectural issues and resubmit. "
        "When execution.started=true, preserve useful prior work but do not assume every partial additive route is good merely because "
        "it was successfully placed. Stored ordinary belt/underground/pole coordinates are execution waypoints, not immutable commands; "
        "adapt those paths to the live geometry, and safely correct AI-owned additive placements that lead into the wrong network. "
        "Do not broadly resurvey unchanged areas."
    )
    text = text.replace(old, new)
    return text, compact


persistence.PersistentRunState.record_plan_execution_step = _record_plan_execution_step_with_ownership
persistence.PersistentRunState.plan_checkpoint_context = _plan_checkpoint_context_adaptive

# Keep forced continuation consistent with the same semantics. A locally revalidated
# PLAN_VALID already sets metrics.plan_validated=True, so this text is only relevant when
# planning genuinely remains incomplete.
persistent.PLAN_GATE_CONTINUE_PROMPT = """PLAN_GATE_INCOMPLETE: the current process does not yet have PLAN_VALID. Continue from the stored architecture instead of restarting discovery. Fix only the remaining validator issues and submit the corrected plan. If startup has already reported that the persisted plan is still PLAN_VALID and mutations are unlocked, do not resubmit it; continue execution instead. Stop only for a genuine external/runtime blocker."""
