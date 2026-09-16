from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PERSISTENT_KNOWLEDGE_TOOLS = {
    "survey_factory_layout",
    "scan_resources",
    "get_power_network_topology",
    "find_buildable_area",
    "summarize_area",
}

MAX_OBSERVATIONS = 24
MAX_OBSERVATION_CHARS = 12_000
MAX_FACTORY_CONTEXT_CHARS = 18_000
MAX_CONTEXT_OBSERVATIONS = 8
MAX_CONTEXT_OBSERVATIONS_PER_TOOL = 2
MAX_RECENT_MUTATIONS = 12
MAX_PLAN_CONTEXT_CHARS = 80_000


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical_goal(goal: str) -> str:
    return " ".join(goal.split())


def goal_fingerprint(goal: str) -> str:
    return hashlib.sha256(_canonical_goal(goal).encode("utf-8")).hexdigest()[:24]


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _is_successful_tool_output(output: str) -> bool:
    text = output.lstrip()
    if text.startswith(("MCP_TOOL_ERROR:", "MCP_TOOL_EXCEPTION:", "INVALID_TOOL_ARGUMENTS:")):
        return False
    if "Cannot execute command. Error:" in text:
        return False
    try:
        payload = json.loads(text)
    except Exception:
        return True
    if not isinstance(payload, dict):
        return True
    if payload.get("success") is False or payload.get("error"):
        return False
    return str(payload.get("status", "")).lower() not in {"error", "failed", "failure", "timeout", "stuck", "no_path"}


def _observation_key(tool_name: str, arguments: dict[str, Any]) -> str:
    rendered = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{tool_name}\n{rendered}".encode("utf-8")).hexdigest()[:20]


def _knowledge_document(player_name: str, document: dict[str, Any]) -> dict[str, Any]:
    if document.get("player_name") == player_name:
        document.setdefault("version", 2)
        document.setdefault("world_revision", 0)
        document.setdefault("observations", [])
        document.setdefault("recent_mutations", [])
        return document
    return {
        "version": 2,
        "player_name": player_name,
        "world_revision": 0,
        "observations": [],
        "recent_mutations": [],
    }


class PersistentRunState:
    """Disk-backed knowledge and plan checkpointing for cloud-agent restarts.

    The files live under the existing local ``state/`` directory, which is git-ignored.
    Knowledge is scoped by configured Factorio player and plan checkpoints additionally
    require an exact normalized goal match before being injected into a later run.

    Persistent observations are intentionally not a chat transcript. They are bounded
    world facts produced by trusted read-only MCP tools. Successful mutations advance a
    monotonically increasing local world revision and retain a short mutation journal so
    later runs can revalidate only the areas that could actually have changed.
    """

    def __init__(self, state_dir: Path, player_name: str, goal: str) -> None:
        self.state_dir = state_dir
        self.player_name = player_name
        self.goal = _canonical_goal(goal)
        self.goal_hash = goal_fingerprint(goal)
        self.knowledge_path = state_dir / "factory_knowledge.json"
        self.plan_path = state_dir / "active_factory_plan.json"

    def record_observation(self, tool_name: str, arguments: dict[str, Any], output: str) -> None:
        if tool_name not in PERSISTENT_KNOWLEDGE_TOOLS or not _is_successful_tool_output(output):
            return

        document = _knowledge_document(self.player_name, _load_json(self.knowledge_path))
        observations = document.get("observations")
        if not isinstance(observations, list):
            observations = []

        key = _observation_key(tool_name, arguments)
        clipped_output = output[:MAX_OBSERVATION_CHARS]
        observation = {
            "key": key,
            "tool": tool_name,
            "arguments": arguments,
            "observed_at": _utc_now(),
            "world_revision": int(document.get("world_revision", 0) or 0),
            "output": clipped_output,
            "truncated": len(output) > len(clipped_output),
        }
        observations = [item for item in observations if isinstance(item, dict) and item.get("key") != key]
        observations.append(observation)
        observations = observations[-MAX_OBSERVATIONS:]

        document.update(
            {
                "version": 2,
                "player_name": self.player_name,
                "updated_at": _utc_now(),
                "observations": observations,
            }
        )
        _write_json(self.knowledge_path, document)

    def mark_world_mutation(self, tool_name: str, arguments: dict[str, Any]) -> None:
        document = _knowledge_document(self.player_name, _load_json(self.knowledge_path))
        revision = int(document.get("world_revision", 0) or 0) + 1
        mutation = {
            "revision": revision,
            "at": _utc_now(),
            "tool": tool_name,
            "arguments": arguments,
        }
        recent_mutations = document.get("recent_mutations")
        if not isinstance(recent_mutations, list):
            recent_mutations = []
        recent_mutations.append(mutation)

        document.update(
            {
                "version": 2,
                "world_revision": revision,
                "last_world_mutation": mutation,
                "recent_mutations": recent_mutations[-MAX_RECENT_MUTATIONS:],
                "updated_at": _utc_now(),
            }
        )
        _write_json(self.knowledge_path, document)

    def has_factory_knowledge(self) -> bool:
        document = _load_json(self.knowledge_path)
        if document.get("player_name") != self.player_name:
            return False
        return any(isinstance(item, dict) for item in document.get("observations", []))

    def factory_context(self) -> tuple[str, int]:
        document = _load_json(self.knowledge_path)
        if document.get("player_name") != self.player_name:
            return "", 0
        observations = [item for item in document.get("observations", []) if isinstance(item, dict)]
        if not observations:
            return "", 0

        # Keep only a small working set in the prompt. The complete bounded observation
        # history remains on disk. We retain at most two scopes from the same broad tool so
        # one repeatedly queried subsystem cannot crowd the entire world memory out.
        selected_newest_first: list[dict[str, Any]] = []
        per_tool: dict[str, int] = {}
        used = 0
        for item in reversed(observations):
            tool = str(item.get("tool", ""))
            if per_tool.get(tool, 0) >= MAX_CONTEXT_OBSERVATIONS_PER_TOOL:
                continue
            rendered = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
            if selected_newest_first and used + len(rendered) > MAX_FACTORY_CONTEXT_CHARS:
                continue
            selected_newest_first.append(item)
            per_tool[tool] = per_tool.get(tool, 0) + 1
            used += len(rendered)
            if len(selected_newest_first) >= MAX_CONTEXT_OBSERVATIONS or used >= MAX_FACTORY_CONTEXT_CHARS:
                break
        selected = list(reversed(selected_newest_first))

        recent_mutations = [
            item
            for item in document.get("recent_mutations", [])
            if isinstance(item, dict)
        ][-MAX_RECENT_MUTATIONS:]
        payload = {
            "player_name": self.player_name,
            "world_revision": int(document.get("world_revision", 0) or 0),
            "recent_mutations": recent_mutations,
            "observations": selected,
        }
        text = (
            "PERSISTENT FACTORY KNOWLEDGE FROM EARLIER RUNS:\n"
            "Reuse these trusted MCP observations instead of rediscovering unchanged architecture. "
            "world_revision increases after successful AI world mutations. An older observation is not globally invalid merely "
            "because its revision is lower: compare the recent mutation journal and revalidate only when a mutation could affect "
            "that observation's area/subsystem, or when the current task needs exact live tile/entity state. Do not repeat broad "
            "surveys merely to reconfirm an unchanged main bus, resource area, assembler district, build area, or power topology.\n"
            + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        )
        return text, len(selected)

    def save_plan_checkpoint(self, attempt: int, plan: Any, validation: dict[str, Any]) -> None:
        existing = _load_json(self.plan_path)
        execution = existing.get("execution") if (
            existing.get("player_name") == self.player_name and existing.get("goal_hash") == self.goal_hash
        ) else None
        if not isinstance(execution, dict):
            execution = {"started": False, "mutation_count": 0, "steps": []}

        document = {
            "version": 1,
            "player_name": self.player_name,
            "goal": self.goal,
            "goal_hash": self.goal_hash,
            "updated_at": _utc_now(),
            "validation_attempt": attempt,
            "status": validation.get("status"),
            "issue_count": validation.get("issue_count"),
            "warning_count": validation.get("warning_count"),
            "issues": validation.get("issues", []),
            "warnings": validation.get("warnings", []),
            "plan": plan if isinstance(plan, dict) else None,
            "execution": execution,
        }
        _write_json(self.plan_path, document)

    def record_plan_execution_step(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        output: str,
        failed: bool,
    ) -> None:
        document = _load_json(self.plan_path)
        if document.get("player_name") != self.player_name or document.get("goal_hash") != self.goal_hash:
            return
        execution = document.get("execution")
        if not isinstance(execution, dict):
            execution = {"started": False, "mutation_count": 0, "steps": []}
        steps = execution.get("steps")
        if not isinstance(steps, list):
            steps = []
        step = {
            "at": _utc_now(),
            "tool": tool_name,
            "arguments": arguments,
            "failed": failed,
            "output": output[:2_000],
        }
        steps.append(step)
        execution.update(
            {
                "started": True,
                "mutation_count": int(execution.get("mutation_count", 0) or 0) + 1,
                "last_step": step,
                "steps": steps[-20:],
            }
        )
        document["execution"] = execution
        document["updated_at"] = _utc_now()
        _write_json(self.plan_path, document)

    def mark_run_finished(self, final_text: str) -> None:
        document = _load_json(self.plan_path)
        if document.get("player_name") != self.player_name or document.get("goal_hash") != self.goal_hash:
            return
        document["last_run_finished_at"] = _utc_now()
        document["last_run_final_text"] = final_text[:4_000]
        _write_json(self.plan_path, document)

    def plan_checkpoint_context(self) -> tuple[str, dict[str, Any] | None]:
        document = _load_json(self.plan_path)
        if document.get("player_name") != self.player_name or document.get("goal_hash") != self.goal_hash:
            return "", None
        if not isinstance(document.get("plan"), dict):
            return "", None

        compact = {
            "validation_attempt": document.get("validation_attempt"),
            "status": document.get("status"),
            "issue_count": document.get("issue_count"),
            "issues": document.get("issues", []),
            "warnings": document.get("warnings", []),
            "plan": document.get("plan"),
            "execution": document.get("execution"),
            "last_run_final_text": document.get("last_run_final_text"),
        }
        rendered = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
        if len(rendered) > MAX_PLAN_CONTEXT_CHARS:
            # Preserve the plan and current issues; trim verbose execution history first.
            execution = compact.get("execution")
            if isinstance(execution, dict):
                execution = dict(execution)
                execution.pop("steps", None)
                compact["execution"] = execution
            compact.pop("last_run_final_text", None)
            rendered = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))

        text = (
            "ACTIVE PLAN CHECKPOINT FROM AN EARLIER RUN OF THIS EXACT GOAL:\n"
            "Continue from this plan instead of starting architecture discovery from zero. "
            "If the checkpoint is PLAN_INVALID, correct the listed issues in the stored plan and resubmit it. "
            "If it is PLAN_VALID, resubmit the stored plan once for fresh live validation before any mutation; previous validation "
            "never unlocks mutations across process restarts. If execution.started=true, assume the prior run may have partially "
            "executed the plan and revalidate only the affected placements/state before continuing. Do not broadly resurvey unchanged areas.\n"
            + rendered
        )
        return text, compact
