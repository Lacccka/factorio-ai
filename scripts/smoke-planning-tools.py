from __future__ import annotations

import asyncio
import json
import sys

from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from factorio_ai.app import Settings, _call_tool, _is_tool_failure, _mcp_env


PROTOTYPES = (
    "inserter",
    "long-handed-inserter",
    "iron-chest",
    "small-electric-pole",
    "medium-electric-pole",
    "electric-mining-drill",
    "steel-furnace",
    "assembling-machine-1",
)


def _parse_ok(output: str) -> dict:
    if _is_tool_failure(output):
        raise RuntimeError(output)
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"tool returned non-JSON output: {output}") from exc
    if not isinstance(payload, dict) or payload.get("success") is False or payload.get("error"):
        raise RuntimeError(output)
    return payload


async def main() -> int:
    load_dotenv()
    settings = Settings.from_env(require_cloud=False)
    if not settings.mcp_project.exists():
        raise FileNotFoundError(
            f"FactorioMCP project not found: {settings.mcp_project}. "
            "Run scripts/bootstrap-factorio-mcp.ps1 first."
        )

    server = StdioServerParameters(
        command="dotnet",
        args=["run", "--project", str(settings.mcp_project), "--no-build"],
        env=_mcp_env(settings),
        cwd=str(settings.mcp_project.parent.parent),
    )

    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            names = {tool.name for tool in listed.tools}
            required = {"get_entity_prototype", "get_item_fuel_info"}
            missing = sorted(required - names)
            if missing:
                raise RuntimeError(
                    "Missing planning smoke-test tools: " + ", ".join(missing) + ". Re-run bootstrap."
                )

            failures: list[str] = []
            for entity in PROTOTYPES:
                output = await _call_tool(
                    session,
                    "get_entity_prototype",
                    {"entityName": entity},
                    settings.tool_result_max_chars,
                )
                try:
                    payload = _parse_ok(output)
                    print(
                        f"PASS prototype {entity}: type={payload.get('type')} "
                        f"size={payload.get('tile_width')}x{payload.get('tile_height')} "
                        f"crafting_speed={payload.get('crafting_speed', '-')} "
                        f"has_burner={payload.get('has_burner', False)}"
                    )
                except Exception as exc:
                    failures.append(f"prototype {entity}: {exc}")
                    print(f"FAIL prototype {entity}: {exc}", file=sys.stderr)

            output = await _call_tool(
                session,
                "get_item_fuel_info",
                {"itemName": "coal"},
                settings.tool_result_max_chars,
            )
            try:
                payload = _parse_ok(output)
                if float(payload.get("fuel_value") or 0) <= 0:
                    raise RuntimeError(f"coal fuel_value is not positive: {output}")
                print(
                    f"PASS fuel coal: fuel_value={payload.get('fuel_value')} "
                    f"fuel_category={payload.get('fuel_category')}"
                )
            except Exception as exc:
                failures.append(f"fuel coal: {exc}")
                print(f"FAIL fuel coal: {exc}", file=sys.stderr)

            if failures:
                print(f"planning smoke test FAILED ({len(failures)} issue(s))", file=sys.stderr)
                return 1

            print("planning smoke test PASSED")
            return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
