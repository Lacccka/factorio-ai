from __future__ import annotations

import json
import os
import sys
from typing import Any

# Import resilient_app first so all existing persistence/resume hardening is installed.
from . import resilient_app as resilient  # noqa: F401
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
_PROMPT_MARKER = "SEMANTIC WORLD MODEL POLICY"

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


# Install semantic memory without changing the stable persistence/checkpoint file format.
# Raw bounded observations remain available locally for debugging/migration, but prompts
# use world_model.json whenever semantic facts are available.
PersistentRunState.record_observation = _record_observation_world_aware
PersistentRunState.mark_world_mutation = _mark_world_mutation_world_aware
PersistentRunState.factory_context = _factory_context_world_aware
base._active_tools = _active_tools_world_aware

if _PROMPT_MARKER not in base.SYSTEM_PROMPT:
    base.SYSTEM_PROMPT += """

SEMANTIC WORLD MODEL POLICY:
When SEMANTIC FACTORY WORLD MODEL is present, treat it as the durable memory of this same save. Do not reconstruct already-known architecture from broad scans. A fact marked may_be_affected_by is not globally invalid: re-check only the local geometry or subsystem implicated by those mutations when the current goal depends on it. The orchestrator intentionally exposes different tool subsets during planning, execution, and verification; do not interpret a temporarily absent unrelated tool as a reason to redesign the task.
"""


def main() -> None:
    base.main()


if __name__ == "__main__":
    main()
