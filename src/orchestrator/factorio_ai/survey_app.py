from __future__ import annotations

import argparse
import asyncio
import sys
from types import SimpleNamespace
from typing import Any

# Install the normal optimized runtime and broader semantic atlas first.
from . import runtime_app as runtime  # noqa: F401
from . import app as base
from .planning import PLAN_TOOL_NAME


_PRIOR_ACTIVE_TOOLS = base._active_tools
_PRIOR_EXECUTE_FUNCTION_CALLS = base._execute_function_calls
_SURVEY_PROMPT_MARKER = "FULL BASE SURVEY MODE"
_SURVEY_BLOCKED_TOOL_NAMES = {PLAN_TOOL_NAME, "ensure_item"}


def _survey_tool_blocked(name: str) -> bool:
    return name in _SURVEY_BLOCKED_TOOL_NAMES or base._is_mutating_tool(name)


def _survey_active_tools(
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
        False,
    )
    return [
        tool
        for tool in tools
        if not _survey_tool_blocked(str(tool.get("name", "")))
    ]


async def _execute_function_calls_survey_only(
    session: Any,
    response: Any,
    settings: base.Settings,
    metrics: base.RunMetrics,
    tool_names: set[str],
    plan_gated: bool,
) -> list[dict[str, Any]]:
    """Hard block world-changing calls even if the model hallucinates a hidden tool."""

    outputs: list[dict[str, Any]] = []
    for item in getattr(response, "output", []) or []:
        if getattr(item, "type", None) != "function_call":
            continue
        name = str(getattr(item, "name", ""))
        if _survey_tool_blocked(name):
            metrics.tool_calls += 1
            metrics.blocked_mutations += 1
            print(f"[survey-only] blocked {name}", file=sys.stderr)
            outputs.append(
                {
                    "type": "function_call_output",
                    "call_id": item.call_id,
                    "output": (
                        "SURVEY_ONLY_TOOL_BLOCKED: this run is strictly read-only. "
                        "Map and inspect the existing factory; do not mutate it, provision inventory, or submit a build plan."
                    ),
                }
            )
            continue

        single_response = SimpleNamespace(output=[item])
        outputs.extend(
            await _PRIOR_EXECUTE_FUNCTION_CALLS(
                session,
                single_response,
                settings,
                metrics,
                tool_names,
                False,
            )
        )
    return outputs


base._active_tools = _survey_active_tools
base._execute_function_calls = _execute_function_calls_survey_only

if _SURVEY_PROMPT_MARKER not in base.SYSTEM_PROMPT:
    base.SYSTEM_PROMPT += """

FULL BASE SURVEY MODE:
This process is a read-only cartography pass over an existing developed Factorio save. Do not craft, ensure/provision inventory, mine, place, rotate, transfer, insert/remove, change recipes/research, submit a factory build plan, or otherwise modify the world. Navigation is allowed only to get within inspection range.

Build a durable mental/semantic atlas of the whole industrial base rather than solving one local production problem. Start from compact global summaries, infer the occupied industrial extent, then inspect the base systematically in overlapping regions until all major districts and long-lived trunks are covered. Do not treat one empty local scan, an empty MCP building-memory result, or absence from the main bus as proof that a production chain does not exist elsewhere.

The survey must identify, as far as available structured tools permit:
- the main bus and other long-lived belt trunks, including direction, approximate coordinate ranges, and sampled carried items;
- resource extraction districts for iron, copper, coal, stone and other active resources;
- smelting districts and which outputs they produce;
- intermediate-production districts such as gears, circuits, steel and other important shared materials;
- major final-production/assembler districts and recognizable recipes;
- electric generation/distribution and major power-network boundaries;
- existing logistics links between upstream production and the main bus/trunks;
- important existing outputs that are produced off-bus and therefore should be tapped/extended rather than rebuilt;
- obvious disconnected/idle legacy structures, clearly distinguished from active infrastructure.

Use multiple regional survey_factory_layout/summarize_area/scan_resources or targeted flow/topology queries when needed. Prefer structured observations over screenshots. Trace important shared materials when their source is ambiguous. Before claiming that an automated source is absent, establish reasonable spatial coverage of the developed base and perform at least one targeted search for that material/production area.

Finish with a concise structured BASE ATLAS: approximate base extent; named/coordinate production districts; main-bus/trunk lanes and carried items; resource and smelting sources; shared intermediates; power topology; and any unresolved unknowns. The purpose is to make later PLAN-GATED runs plan against the existing factory rather than rediscovering or duplicating upstream production.
"""


DEFAULT_SURVEY_GOAL = (
    "Изучи всю существующую базу целиком и составь подробную карту её архитектуры. "
    "Ничего не строй и не меняй. Систематически обследуй main bus и другие trunk-линии, "
    "добычу ресурсов, плавку, промежуточное производство, основные assembler-зоны, электросеть "
    "и связи между ними. Особенно установи, где и как производятся и транспортируются iron-plate, "
    "copper-plate, steel-plate, iron-gear-wheel, electronic-circuit, coal, stone и stone-brick. "
    "Не делай вывод об отсутствии производства по одному локальному скану. В конце дай структурированный "
    "BASE ATLAS с координатами/диапазонами всех основных районов и неизвестными местами, которые не удалось подтвердить."
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Strictly read-only whole-base survey using the optimized Factorio AI runtime"
    )
    parser.add_argument("goal", nargs="?", default=DEFAULT_SURVEY_GOAL)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    try:
        raise SystemExit(asyncio.run(base.run(args.goal, plan_gated=False)))
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        base._print_exception(exc)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
