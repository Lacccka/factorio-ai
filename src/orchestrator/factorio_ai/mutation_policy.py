from __future__ import annotations

import json
from typing import Any


def classify_mutation_domains(tool_name: str, arguments: dict[str, Any]) -> set[str]:
    """Return semantic world-model domains a successful mutation can invalidate.

    Inventory-only operations are handled first so crafting a belt does not incorrectly
    invalidate the remembered physical bus merely because the item name contains "belt".
    """
    if tool_name in {"craft", "ensure_item"} or tool_name.startswith(
        ("transfer_", "insert_", "remove_", "pickup_", "drop_")
    ):
        return {"inventory"}
    if tool_name.startswith(("research_", "start_research", "cancel_research")):
        return {"research"}

    text = (tool_name + " " + json.dumps(arguments, ensure_ascii=False, sort_keys=True)).lower()
    domains: set[str] = set()

    if tool_name.startswith(("place_", "mine_", "rotate_", "revive_")) or tool_name == "clear_remnants":
        domains.add("geometry")
    if any(token in text for token in ("belt", "splitter", "inserter", "loader", "chest", "logistic")):
        domains.update({"geometry", "logistics"})
    if any(token in text for token in ("electric", "pole", "substation", "power", "switch")) or tool_name.startswith(
        ("connect_", "disconnect_")
    ):
        domains.update({"power", "geometry"})
    if any(
        token in text
        for token in (
            "assembler",
            "assembling",
            "furnace",
            "beacon",
            "recipe",
            "chemical",
            "refinery",
            "drill",
            "lab",
        )
    ):
        domains.update({"production", "geometry"})
    if any(token in text for token in ("resource", "ore", "mining-drill", "pumpjack")):
        domains.add("resources")

    # Unknown successful world mutations should conservatively invalidate local geometry,
    # not every architectural fact in the save.
    return domains or {"geometry"}
