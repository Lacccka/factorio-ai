from __future__ import annotations

from typing import Any

from . import persistence
from . import persistent_app as persistent


_ORIGINAL_PLAN_CHECKPOINT_CONTEXT = persistence.PersistentRunState.plan_checkpoint_context


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
        "When execution.started=true, preserve successful prior work. Stored ordinary belt/underground/pole coordinates are execution "
        "waypoints, not immutable commands; adapt those additive paths to the live local geometry while preserving the plan architecture. "
        "Do not broadly resurvey unchanged areas."
    )
    text = text.replace(old, new)
    return text, compact


persistence.PersistentRunState.plan_checkpoint_context = _plan_checkpoint_context_adaptive

# Keep forced continuation consistent with the same semantics. A locally revalidated
# PLAN_VALID already sets metrics.plan_validated=True, so this text is only relevant when
# planning genuinely remains incomplete.
persistent.PLAN_GATE_CONTINUE_PROMPT = """PLAN_GATE_INCOMPLETE: the current process does not yet have PLAN_VALID. Continue from the stored architecture instead of restarting discovery. Fix only the remaining validator issues and submit the corrected plan. If startup has already reported that the persisted plan is still PLAN_VALID and mutations are unlocked, do not resubmit it; continue execution instead. Stop only for a genuine external/runtime blocker."""
