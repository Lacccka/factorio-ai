from __future__ import annotations

import copy
import json
import os
import sys
from typing import Any, Awaitable, Callable

# Import resilient_app first so all existing persistence/resume hardening is installed.
from . import resilient_app as resilient
from . import app as base
from . import world_model as world_model_module
from .mutation_policy import classify_mutation_domains
from .persistence import PersistentRunState
from .phase_tools import filter_tools_for_phase, phase_for
from .world_model import SemanticWorldModel


_ORIGINAL_RECORD_OBSERVATION = PersistentRunState.record_observation
_ORIGINAL_MARK_WORLD_MUTATION = PersistentRunState.mark_world_mutation
_ORIGINAL_FACTORY_CONTEXT = PersistentRunState.factory_context
_PRIOR_ACTIVE_TOOLS = base._active_tools
_PRIOR_CALL_TOOL = base._call_tool
_PRIOR_VALIDATE_FACTORY_PLAN = base.validate_factory_plan
_PRIOR_IS_TOOL_FAILURE = base._is_tool_failure
_PRIOR_PERSISTENT_EXECUTE = resilient._PERSISTENT_EXECUTE_FUNCTION_CALLS
_PROMPT_MARKER = "SEMANTIC WORLD MODEL POLICY"
_PARTIAL_RESUME_PROMPT_MARKER = "PARTIAL EXECUTION RESUME POLICY"
_MUTATION_BUDGET_MARKER = "\n\nMUTATION_BUDGET_REACHED:"

CallMcp = Callable[[str, dict[str, Any]], Awaitable[str]]

# SemanticWorldModel.record_mutation resolves this function from its module at runtime.
# Keep the compatibility implementation in world_model.py, but install the stricter policy
# here so inventory-only actions cannot stale physical architecture facts.
world_model_module.mutation_domains = classify_mutation_domains


def _knowledge_document(store: PersistentRunState) -> dict[str, Any]:
    try:
        value = json.loads(store.knowledge_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return value if isinstance(value, dict) else {}


def _semantic_store(store: PersistentRunState) -> SemanticWorldModel:
    return SemanticWorldModel(store.state_dir, store.player_name)


def _record_observation_world_aware(
    self: PersistentRunState,
    tool_name: str,
    arguments: dict[str, Any],
    output: str,
) -> None:
    _ORIGINAL_RECORD_OBSERVATION(self, tool_name, arguments, output)
    document = _knowledge_document(self)
    revision = int(document.get("world_revision", 0) or 0)
    _semantic_store(self).observe(tool_name, arguments, output, revision)


def _mark_world_mutation_world_aware(
    self: PersistentRunState,
    tool_name: str,
    arguments: dict[str, Any],
) -> None:
    _ORIGINAL_MARK_WORLD_MUTATION(self, tool_name, arguments)
    document = _knowledge_document(self)
    revision = int(document.get("world_revision", 0) or 0)
    last = document.get("last_world_mutation")
    at = str(last.get("at", "")) if isinstance(last, dict) else ""
    _semantic_store(self).record_mutation(tool_name, arguments, revision, at or None)


def _factory_context_world_aware(self: PersistentRunState) -> tuple[str, int]:
    world = _semantic_store(self)
    if world.is_empty():
        imported = world.import_legacy(_knowledge_document(self))
        if imported:
            print(f"[world-model] migrated semantic facts from legacy observations={imported}", file=sys.stderr)

    context, count = world.context()
    if context:
        return context, count
    return _ORIGINAL_FACTORY_CONTEXT(self)


def _phase_filter_enabled() -> bool:
    raw = os.getenv("AGENT_PHASE_TOOL_FILTER", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _active_tools_world_aware(
    all_tools: list[dict[str, Any]],
    gated_tools: list[dict[str, Any]],
    read_only_tools: list[dict[str, Any]],
    metrics: base.RunMetrics,
    plan_gated: bool,
) -> list[dict[str, Any]]:
    tools = _PRIOR_ACTIVE_TOOLS(
        all_tools,
        gated_tools,
        read_only_tools,
        metrics,
        plan_gated,
    )
    if not _phase_filter_enabled():
        return tools

    phase = phase_for(metrics, plan_gated)
    filtered = filter_tools_for_phase(tools, phase, base._is_mutating_tool)
    previous_phase = getattr(metrics, "_agent_phase", None)
    previous_count = getattr(metrics, "_agent_phase_tool_count", None)
    if previous_phase != phase or previous_count != len(filtered):
        print(
            f"[tool-phase] phase={phase} tools={len(filtered)}/{len(tools)}",
            file=sys.stderr,
        )
        setattr(metrics, "_agent_phase", phase)
        setattr(metrics, "_agent_phase_tool_count", len(filtered))
    return filtered


def _parse_tool_json(output: str) -> dict[str, Any] | None:
    text = output.split(_MUTATION_BUDGET_MARKER, 1)[0].strip()
    if text.startswith(("MCP_TOOL_ERROR:", "MCP_TOOL_EXCEPTION:", "INVALID_TOOL_ARGUMENTS:")):
        return None
    try:
        value = json.loads(text)
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def _is_out_of_range_failure(output: str) -> bool:
    payload = _parse_tool_json(output)
    return isinstance(payload, dict) and str(payload.get("error", "")).lower() == "out_of_range"


def _is_tool_failure_budget_aware(output: str) -> bool:
    # The base executor appends MUTATION_BUDGET_REACHED after the tool JSON. Parse the
    # actual tool result rather than letting the suffix turn a real failure into invalid JSON.
    clean = output.split(_MUTATION_BUDGET_MARKER, 1)[0]
    return _PRIOR_IS_TOOL_FAILURE(clean)


def _matching_entity(payload: dict[str, Any], desired: dict[str, Any]) -> dict[str, Any] | None:
    name = str(desired.get("entity_name") or desired.get("entityName") or "")
    try:
        x = float(desired["x"])
        y = float(desired["y"])
    except (KeyError, TypeError, ValueError):
        return None
    direction = str(desired.get("direction", "")).lower()

    for entity in payload.get("entities", []) or []:
        if not isinstance(entity, dict) or str(entity.get("name", "")) != name:
            continue
        try:
            ex = float(entity.get("x"))
            ey = float(entity.get("y"))
        except (TypeError, ValueError):
            continue
        if abs(ex - x) > 0.11 or abs(ey - y) > 0.11:
            continue
        actual_direction = str(entity.get("direction", "")).lower()
        if direction and actual_direction and actual_direction != direction:
            continue
        return entity
    return None


async def _placement_already_satisfied(call_mcp: CallMcp, placement: dict[str, Any]) -> bool:
    try:
        x = float(placement["x"])
        y = float(placement["y"])
    except (KeyError, TypeError, ValueError):
        return False
    payload = _parse_tool_json(
        await call_mcp(
            "get_nearby_entities",
            {"radius": 2.0, "centerX": x, "centerY": y},
        )
    )
    return isinstance(payload, dict) and _matching_entity(payload, placement) is not None


async def _validate_factory_plan_idempotent(
    plan: Any,
    call_mcp: CallMcp,
    available_tool_names: set[str],
) -> dict[str, Any]:
    """Treat exact already-built plan placements as satisfied during resume validation.

    A partially executed PLAN_VALID normally makes surface.can_place_entity return false for
    placements that now exist. We only relax those specific live collisions after confirming
    the exact entity name, position and direction in the real world, then rerun the complete
    deterministic validator. Unrelated blockers still fail normally.
    """

    validation = await _PRIOR_VALIDATE_FACTORY_PLAN(plan, call_mcp, available_tool_names)
    if not isinstance(plan, dict) or "get_nearby_entities" not in available_tool_names:
        return validation

    blocked_ids = {
        str(issue.get("placement_id", ""))
        for issue in (validation.get("issues") or [])
        if isinstance(issue, dict)
        and str(issue.get("code", "")) == "cannot_place_entity"
        and str(issue.get("placement_id", ""))
    }
    if not blocked_ids:
        return validation

    resumed_plan = copy.deepcopy(plan)
    placements = resumed_plan.get("placements")
    if not isinstance(placements, list):
        return validation

    satisfied: list[str] = []
    for placement in placements:
        if not isinstance(placement, dict):
            continue
        placement_id = str(placement.get("id", ""))
        if placement_id not in blocked_ids:
            continue
        if await _placement_already_satisfied(call_mcp, placement):
            placement["allow_replace_existing"] = True
            satisfied.append(placement_id)

    if not satisfied:
        return validation

    rerun = await _PRIOR_VALIDATE_FACTORY_PLAN(resumed_plan, call_mcp, available_tool_names)
    warnings = [item for item in (rerun.get("warnings") or []) if isinstance(item, dict)]
    warnings.append(
        {
            "code": "placements_already_satisfied",
            "message": (
                "Existing live entities exactly match planned placements and are treated as "
                "already completed during partial-execution resume."
            ),
            "placement_ids": sorted(satisfied),
        }
    )
    rerun["warnings"] = warnings
    rerun["warning_count"] = len(warnings)
    print(
        f"[resume] confirmed already-built plan placements={len(satisfied)}",
        file=sys.stderr,
    )
    return rerun


async def _call_tool_idempotent(
    session: Any,
    name: str,
    arguments: dict[str, Any],
    max_chars: int,
) -> str:
    # Repeating a completed place_entity after process restart should be a no-op rather
    # than a collision/failure. Confirm exact live state first; mismatches still hit MCP.
    if name == "place_entity":
        entity_name = str(arguments.get("entityName") or arguments.get("entity_name") or "")
        try:
            x = float(arguments["x"])
            y = float(arguments["y"])
        except (KeyError, TypeError, ValueError):
            x = y = float("nan")
        if entity_name and x == x and y == y:
            nearby = _parse_tool_json(
                await _PRIOR_CALL_TOOL(
                    session,
                    "get_nearby_entities",
                    {"radius": 2.0, "centerX": x, "centerY": y},
                    min(max_chars, 12_000),
                )
            )
            desired = {
                "entity_name": entity_name,
                "x": x,
                "y": y,
                "direction": arguments.get("direction", ""),
            }
            if isinstance(nearby, dict) and _matching_entity(nearby, desired) is not None:
                return json.dumps(
                    {
                        "success": True,
                        "status": "already_present",
                        "entity": entity_name,
                        "x": x,
                        "y": y,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
    return await _PRIOR_CALL_TOOL(session, name, arguments, max_chars)


async def _persistent_execute_range_tolerant(
    session: Any,
    response: Any,
    settings: base.Settings,
    metrics: base.RunMetrics,
    tool_names: set[str],
    plan_gated: bool,
) -> list[dict[str, Any]]:
    before_failed = metrics.failed_mutations
    outputs = await _PRIOR_PERSISTENT_EXECUTE(
        session,
        response,
        settings,
        metrics,
        tool_names,
        plan_gated,
    )

    proximity_failures = sum(
        1
        for item in outputs
        if isinstance(item, dict) and _is_out_of_range_failure(str(item.get("output", "")))
    )
    if not proximity_failures:
        return outputs

    newly_counted = max(0, metrics.failed_mutations - before_failed)
    forgiven = min(proximity_failures, newly_counted)
    metrics.failed_mutations = max(0, metrics.failed_mutations - forgiven)

    # out_of_range means the world was untouched and the normal recovery is simply to
    # walk closer. Do not let repeated reach mistakes trip the anti-thrashing failure cap.
    if metrics.mutation_budget_exhausted and not base._budget_exhausted(settings, metrics):
        metrics.mutation_budget_exhausted = False
        for item in outputs:
            if not isinstance(item, dict):
                continue
            text = str(item.get("output", ""))
            if _is_out_of_range_failure(text) and _MUTATION_BUDGET_MARKER in text:
                item["output"] = text.split(_MUTATION_BUDGET_MARKER, 1)[0]
    print(
        f"[budget] ignored out_of_range failures={forgiven}; failed_mutations={metrics.failed_mutations}",
        file=sys.stderr,
    )
    return outputs


# Install semantic memory without changing the stable persistence/checkpoint file format.
# Raw bounded observations remain available locally for debugging/migration, but prompts
# use world_model.json whenever semantic facts are available.
PersistentRunState.record_observation = _record_observation_world_aware
PersistentRunState.mark_world_mutation = _mark_world_mutation_world_aware
PersistentRunState.factory_context = _factory_context_world_aware
base._active_tools = _active_tools_world_aware
base._call_tool = _call_tool_idempotent
base._is_tool_failure = _is_tool_failure_budget_aware
base.validate_factory_plan = _validate_factory_plan_idempotent
resilient.validate_factory_plan = _validate_factory_plan_idempotent
resilient._PERSISTENT_EXECUTE_FUNCTION_CALLS = _persistent_execute_range_tolerant

if _PROMPT_MARKER not in base.SYSTEM_PROMPT:
    base.SYSTEM_PROMPT += """

SEMANTIC WORLD MODEL POLICY:
When SEMANTIC FACTORY WORLD MODEL is present, treat it as the durable memory of this same save. Do not reconstruct already-known architecture from broad scans. A fact marked may_be_affected_by is not globally invalid: re-check only the local geometry or subsystem implicated by those mutations when the current goal depends on it. The orchestrator intentionally exposes different tool subsets during planning, execution, and verification; do not interpret a temporarily absent unrelated tool as a reason to redesign the task.
"""

if _PARTIAL_RESUME_PROMPT_MARKER not in base.SYSTEM_PROMPT:
    base.SYSTEM_PROMPT += """

PARTIAL EXECUTION RESUME POLICY:
When an exact-goal checkpoint shows execution.started=true, continue the approved design from the live world instead of rebuilding completed placements. Matching planned entities that already exist are completed work, not placement failures. Finish missing material routes, extractors, fuel delivery, power distribution and outputs before declaring success. A production block is complete only after live verification shows the intended ingredients can reach it, required power/fuel is available, and its output is actually produced or moving to the intended belt/buffer. Do not stop merely because machines and inserters have been placed.
"""


def main() -> None:
    base.main()


if __name__ == "__main__":
    main()
