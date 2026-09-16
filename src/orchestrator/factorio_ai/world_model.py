from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


WORLD_MODEL_VERSION = 1
MAX_SECTION_ENTRIES = 4
MAX_CONTEXT_ENTRIES_PER_SECTION = 2
MAX_CONTEXT_CHARS = 14_000
MAX_RECENT_MUTATIONS = 16
MAX_RELEVANT_MUTATIONS_PER_FACT = 4

_TOOL_SECTIONS = {
    "survey_factory_layout": ("architecture", {"geometry", "logistics", "production"}),
    "scan_resources": ("resources", {"resources"}),
    "get_power_network_topology": ("power", {"power", "geometry"}),
    "find_buildable_area": ("build_areas", {"geometry"}),
    "summarize_area": ("areas", {"geometry", "logistics", "production", "power"}),
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_json(path: Path) -> dict[str, Any]:
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


def _scope_key(tool_name: str, arguments: dict[str, Any]) -> str:
    encoded = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{tool_name}\n{encoded}".encode("utf-8")).hexdigest()[:16]


def _compact_value(value: Any, *, depth: int = 0, list_limit: int = 12) -> Any:
    """Bound structured MCP output without turning it into an opaque text clipping.

    Semantic memory is deliberately lossy: detailed live state can always be queried again.
    Keeping JSON structure is more useful to the planner than carrying old raw tool output.
    """
    if depth >= 4:
        if isinstance(value, (dict, list)):
            return "[nested data omitted]"
        return value
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if key in {"success", "status"} and item in {True, "ok", "success"}:
                continue
            compacted = _compact_value(item, depth=depth + 1, list_limit=list_limit)
            if compacted in (None, "", [], {}):
                continue
            result[str(key)] = compacted
        return result
    if isinstance(value, list):
        compacted = [
            _compact_value(item, depth=depth + 1, list_limit=list_limit)
            for item in value[:list_limit]
        ]
        if len(value) > list_limit:
            compacted.append({"omitted_items": len(value) - list_limit})
        return compacted
    if isinstance(value, str):
        return value if len(value) <= 500 else value[:497] + "..."
    return value


def _compact_survey(payload: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in (
        "center_x",
        "center_y",
        "radius",
        "belt_entity_count",
        "reported_runs",
        "assembler_count",
    ):
        if key in payload:
            result[key] = payload[key]

    runs: list[dict[str, Any]] = []
    for run in payload.get("belt_runs", [])[:16] if isinstance(payload.get("belt_runs"), list) else []:
        if not isinstance(run, dict):
            continue
        item = {
            key: run.get(key)
            for key in (
                "axis",
                "flow_direction",
                "fixed_coordinate",
                "start_coordinate",
                "end_coordinate",
                "tile_count",
                "belt",
                "sample_x",
                "sample_y",
            )
            if run.get(key) is not None
        }
        sample_items = run.get("sample_items")
        if isinstance(sample_items, list) and sample_items:
            item["sample_items"] = _compact_value(sample_items, list_limit=6)
        runs.append(item)
    if runs:
        result["belt_runs"] = runs

    zones: list[dict[str, Any]] = []
    for zone in payload.get("assembler_zones", [])[:8] if isinstance(payload.get("assembler_zones"), list) else []:
        if not isinstance(zone, dict):
            continue
        item = {
            key: zone.get(key)
            for key in ("assembler_count", "min_x", "max_x", "min_y", "max_y")
            if zone.get(key) is not None
        }
        recipes = zone.get("recipes")
        if isinstance(recipes, list) and recipes:
            item["recipes"] = _compact_value(recipes, list_limit=6)
        zones.append(item)
    if zones:
        result["assembler_zones"] = zones
    return result


def semantic_snapshot(tool_name: str, output: str) -> tuple[str, set[str], dict[str, Any]] | None:
    metadata = _TOOL_SECTIONS.get(tool_name)
    if metadata is None:
        return None
    try:
        payload = json.loads(output)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("success") is False or payload.get("error"):
        return None

    section, domains = metadata
    if tool_name == "survey_factory_layout":
        facts = _compact_survey(payload)
    else:
        facts = _compact_value(payload, list_limit=16)
    if not isinstance(facts, dict) or not facts:
        return None
    return section, set(domains), facts


def mutation_domains(tool_name: str, arguments: dict[str, Any]) -> set[str]:
    text = (tool_name + " " + json.dumps(arguments, ensure_ascii=False, sort_keys=True)).lower()
    domains: set[str] = set()

    if tool_name.startswith(("place_", "mine_", "rotate_", "revive_")) or tool_name == "clear_remnants":
        domains.add("geometry")
    if any(token in text for token in ("belt", "splitter", "inserter", "loader", "chest", "logistic")):
        domains.update({"geometry", "logistics"})
    if any(token in text for token in ("electric", "pole", "substation", "power", "switch")) or tool_name.startswith(("connect_", "disconnect_")):
        domains.update({"power", "geometry"})
    if any(token in text for token in ("assembler", "assembling", "furnace", "beacon", "recipe", "chemical", "refinery", "drill", "lab")):
        domains.update({"production", "geometry"})
    if any(token in text for token in ("resource", "ore", "mining-drill", "pumpjack")):
        domains.add("resources")
    if tool_name.startswith(("research_", "start_research", "cancel_research")):
        domains.add("research")
    if tool_name in {"craft", "ensure_item"} or tool_name.startswith(("transfer_", "insert_", "remove_", "pickup_", "drop_")):
        domains.add("inventory")

    # Unknown successful world mutations should conservatively invalidate local geometry,
    # not every architectural fact in the world.
    if not domains:
        domains.add("geometry")
    return domains


def _coordinate_points(value: Any, points: list[dict[str, float]]) -> None:
    if len(points) >= 6:
        return
    if isinstance(value, dict):
        x = value.get("x")
        y = value.get("y")
        if isinstance(x, (int, float)) and isinstance(y, (int, float)):
            point = {"x": float(x), "y": float(y)}
            if point not in points:
                points.append(point)
        for item in value.values():
            _coordinate_points(item, points)
    elif isinstance(value, list):
        for item in value:
            _coordinate_points(item, points)


def mutation_scope(arguments: dict[str, Any]) -> dict[str, Any]:
    points: list[dict[str, float]] = []
    _coordinate_points(arguments, points)
    result: dict[str, Any] = {}
    if points:
        result["points"] = points
    for key in ("entityName", "entity_name", "recipe", "item", "itemName", "technology"):
        if key in arguments and isinstance(arguments[key], (str, int, float)):
            result[key] = arguments[key]
    return result


class SemanticWorldModel:
    def __init__(self, state_dir: Path, player_name: str) -> None:
        self.path = state_dir / "world_model.json"
        self.player_name = player_name

    def _document(self) -> dict[str, Any]:
        document = _read_json(self.path)
        if document.get("player_name") != self.player_name:
            return {
                "version": WORLD_MODEL_VERSION,
                "player_name": self.player_name,
                "world_revision": 0,
                "sections": {},
                "recent_mutations": [],
            }
        document.setdefault("version", WORLD_MODEL_VERSION)
        document.setdefault("world_revision", 0)
        document.setdefault("sections", {})
        document.setdefault("recent_mutations", [])
        return document

    def is_empty(self) -> bool:
        document = self._document()
        sections = document.get("sections")
        if not isinstance(sections, dict):
            return True
        return not any(isinstance(entries, dict) and entries for entries in sections.values())

    def observe(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        output: str,
        world_revision: int,
        observed_at: str | None = None,
    ) -> bool:
        snapshot = semantic_snapshot(tool_name, output)
        if snapshot is None:
            return False
        section, domains, facts = snapshot
        document = self._document()
        sections = document.get("sections")
        if not isinstance(sections, dict):
            sections = {}
        entries = sections.get(section)
        if not isinstance(entries, dict):
            entries = {}

        key = _scope_key(tool_name, arguments)
        entries[key] = {
            "tool": tool_name,
            "scope": _compact_value(arguments, list_limit=8),
            "observed_at": observed_at or _utc_now(),
            "observed_revision": int(world_revision),
            "domains": sorted(domains),
            "facts": facts,
        }
        ordered = sorted(
            entries.items(),
            key=lambda pair: str(pair[1].get("observed_at", "")) if isinstance(pair[1], dict) else "",
        )[-MAX_SECTION_ENTRIES:]
        sections[section] = dict(ordered)
        document.update(
            {
                "version": WORLD_MODEL_VERSION,
                "player_name": self.player_name,
                "world_revision": max(int(document.get("world_revision", 0) or 0), int(world_revision)),
                "sections": sections,
                "updated_at": _utc_now(),
            }
        )
        _write_json(self.path, document)
        return True

    def record_mutation(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        world_revision: int,
        at: str | None = None,
    ) -> None:
        document = self._document()
        mutations = document.get("recent_mutations")
        if not isinstance(mutations, list):
            mutations = []
        mutation = {
            "revision": int(world_revision),
            "at": at or _utc_now(),
            "tool": tool_name,
            "domains": sorted(mutation_domains(tool_name, arguments)),
            "scope": mutation_scope(arguments),
        }
        mutations = [
            item
            for item in mutations
            if not (isinstance(item, dict) and int(item.get("revision", -1) or -1) == int(world_revision))
        ]
        mutations.append(mutation)
        document.update(
            {
                "version": WORLD_MODEL_VERSION,
                "player_name": self.player_name,
                "world_revision": max(int(document.get("world_revision", 0) or 0), int(world_revision)),
                "recent_mutations": mutations[-MAX_RECENT_MUTATIONS:],
                "updated_at": _utc_now(),
            }
        )
        _write_json(self.path, document)

    def import_legacy(self, knowledge_document: dict[str, Any]) -> int:
        """One-way lazy migration from the old bounded raw-observation cache."""
        if knowledge_document.get("player_name") != self.player_name:
            return 0
        imported = 0
        default_revision = int(knowledge_document.get("world_revision", 0) or 0)
        for observation in knowledge_document.get("observations", []) or []:
            if not isinstance(observation, dict):
                continue
            tool = str(observation.get("tool", ""))
            args = observation.get("arguments")
            output = observation.get("output")
            if not isinstance(args, dict) or not isinstance(output, str):
                continue
            revision = int(observation.get("world_revision", default_revision) or 0)
            if self.observe(tool, args, output, revision, str(observation.get("observed_at", "")) or None):
                imported += 1

        for mutation in knowledge_document.get("recent_mutations", []) or []:
            if not isinstance(mutation, dict):
                continue
            tool = str(mutation.get("tool", ""))
            args = mutation.get("arguments")
            revision = mutation.get("revision")
            if not tool or not isinstance(args, dict) or not isinstance(revision, int):
                continue
            self.record_mutation(tool, args, revision, str(mutation.get("at", "")) or None)
        return imported

    def context(self) -> tuple[str, int]:
        document = self._document()
        sections = document.get("sections")
        if not isinstance(sections, dict):
            return "", 0
        mutations = [item for item in document.get("recent_mutations", []) if isinstance(item, dict)]

        selected_sections: dict[str, list[dict[str, Any]]] = {}
        fact_count = 0
        used = 0
        for section_name in ("architecture", "resources", "power", "build_areas", "areas"):
            entries = sections.get(section_name)
            if not isinstance(entries, dict):
                continue
            ordered = sorted(
                (entry for entry in entries.values() if isinstance(entry, dict)),
                key=lambda entry: str(entry.get("observed_at", "")),
                reverse=True,
            )
            selected: list[dict[str, Any]] = []
            for entry in ordered[:MAX_CONTEXT_ENTRIES_PER_SECTION]:
                observed_revision = int(entry.get("observed_revision", 0) or 0)
                domains = {str(value) for value in entry.get("domains", []) if value}
                relevant = [
                    mutation
                    for mutation in mutations
                    if int(mutation.get("revision", 0) or 0) > observed_revision
                    and domains.intersection(str(value) for value in mutation.get("domains", []) if value)
                ][-MAX_RELEVANT_MUTATIONS_PER_FACT:]
                compact = {
                    "observed_revision": observed_revision,
                    "scope": entry.get("scope", {}),
                    "facts": entry.get("facts", {}),
                }
                if relevant:
                    compact["may_be_affected_by"] = relevant
                rendered = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
                if selected and used + len(rendered) > MAX_CONTEXT_CHARS:
                    continue
                selected.append(compact)
                used += len(rendered)
                fact_count += 1
                if used >= MAX_CONTEXT_CHARS:
                    break
            if selected:
                selected_sections[section_name] = selected
            if used >= MAX_CONTEXT_CHARS:
                break

        if not selected_sections:
            return "", 0
        payload = {
            "world_revision": int(document.get("world_revision", 0) or 0),
            "sections": selected_sections,
        }
        text = (
            "SEMANTIC FACTORY WORLD MODEL FROM EARLIER RUNS:\n"
            "Treat this as durable structured knowledge of the same Factorio world, not as chat history. "
            "Reuse facts that are unrelated to later mutations. Entries with may_be_affected_by need only local/subsystem "
            "revalidation when the current goal depends on them; do not resurvey the whole factory. Human players may also "
            "change the world, so exact live placement/occupancy must still be checked immediately before consequential builds.\n"
            + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        )
        return text, fact_count
