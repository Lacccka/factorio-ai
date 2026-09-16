from __future__ import annotations

import sys
from typing import Any

from . import app as base
from . import persistence
from . import resilient_app as resilient


CURRENT_PLAN_POLICY_REVISION = 2
_PROMPT_MARKER = "DEMAND-DRIVEN DEFAULT RATES"
_ORIGINAL_SAVE_PLAN_CHECKPOINT = persistence.PersistentRunState.save_plan_checkpoint
_PRIOR_REVALIDATE_CHECKPOINT = resilient._revalidate_and_repair_checkpoint


def _save_plan_checkpoint_versioned(
    self: persistence.PersistentRunState,
    attempt: int,
    plan: Any,
    validation: dict[str, Any],
) -> None:
    # Preserve the private ownership ledger across plan validation/resubmission. The base
    # checkpoint writer intentionally reconstructs the public checkpoint document, which
    # otherwise drops proof that a belt/pole was AI-created and safe to correct later.
    existing = persistence._load_json(self.plan_path)
    owned = existing.get("owned_additive") if isinstance(existing.get("owned_additive"), dict) else None

    _ORIGINAL_SAVE_PLAN_CHECKPOINT(self, attempt, plan, validation)

    document = persistence._load_json(self.plan_path)
    document["plan_policy_revision"] = CURRENT_PLAN_POLICY_REVISION
    if owned:
        document["owned_additive"] = owned
    persistence._write_json(self.plan_path, document)


def _raw_policy_revision(store: persistence.PersistentRunState) -> int:
    document = persistence._load_json(store.plan_path)
    try:
        return int(document.get("plan_policy_revision") or 0)
    except (TypeError, ValueError):
        return 0


async def _revalidate_checkpoint_policy_aware(
    session: Any,
    settings: base.Settings,
    metrics: base.RunMetrics,
    tool_names: set[str],
    store: persistence.PersistentRunState,
    plan_gated: bool,
) -> None:
    if plan_gated:
        _context, checkpoint = store.plan_checkpoint_context()
        if isinstance(checkpoint, dict) and isinstance(checkpoint.get("plan"), dict):
            found_revision = _raw_policy_revision(store)
            if found_revision != CURRENT_PLAN_POLICY_REVISION:
                try:
                    previous_attempt = int(checkpoint.get("validation_attempt") or 0)
                except (TypeError, ValueError):
                    previous_attempt = 0
                metrics.plan_validation_attempts = max(metrics.plan_validation_attempts, previous_attempt) + 1
                validation = {
                    "valid": False,
                    "status": "PLAN_INVALID",
                    "issue_count": 1,
                    "warning_count": 0,
                    "issues": [
                        {
                            # Include "rate" in the issue code so resume phase exposes the
                            # narrow recipe/rate tools needed to repair this policy change.
                            "code": "planning_rate_policy_revision_changed",
                            "message": (
                                "The stored plan predates the current quality policy. Re-evaluate architecture before execution. "
                                "In particular, when the user did not request a numeric throughput, do not choose a downstream "
                                "machine's theoretical maximum as the target rate and then expand upstream to satisfy it. Choose "
                                "a demand-driven practical sustained rate, prefer existing upstream capacity, and preserve only "
                                "live partial work that remains compatible with the revised architecture. Submit the full revised plan."
                            ),
                            "stored_revision": found_revision,
                            "required_revision": CURRENT_PLAN_POLICY_REVISION,
                        }
                    ],
                    "warnings": [],
                    "message": "Stored PLAN_VALID cannot be resumed under the newer planning-quality policy; revise and resubmit.",
                }
                metrics.plan_validated = False
                metrics.validated_plan = None
                setattr(metrics, "_latest_plan_validation", validation)
                setattr(metrics, "_fresh_plan_validation_seen", True)
                setattr(metrics, "_resume_repair_mode", True)
                setattr(metrics, "_resume_diagnostic_calls", 0)
                store.save_plan_checkpoint(metrics.plan_validation_attempts, checkpoint["plan"], validation)
                print(
                    "[plan-policy] invalidated stale checkpoint "
                    f"revision={found_revision}->{CURRENT_PLAN_POLICY_REVISION}; architectural resubmission required",
                    file=sys.stderr,
                )
                return

    await _PRIOR_REVALIDATE_CHECKPOINT(
        session,
        settings,
        metrics,
        tool_names,
        store,
        plan_gated,
    )


persistence.PersistentRunState.save_plan_checkpoint = _save_plan_checkpoint_versioned
resilient._revalidate_and_repair_checkpoint = _revalidate_checkpoint_policy_aware

if _PROMPT_MARKER not in base.SYSTEM_PROMPT:
    base.SYSTEM_PROMPT += """

DEMAND-DRIVEN DEFAULT RATES:
When the user has not specified a numeric production rate, never default target_rate_per_second to the theoretical maximum of an available machine merely because that capacity exists. Start from the actual utility/demand of the requested product and the live capacity of reusable upstream infrastructure. Prefer a modest sustained rate that satisfies the requested automation without unnecessary upstream expansion. Expand an existing upstream district only when the chosen demand-driven target actually exceeds its verified live capacity. Machine maximum is headroom, not a design target.
"""
