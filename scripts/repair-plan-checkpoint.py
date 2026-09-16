from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from factorio_ai.app import Settings, _call_tool, _mcp_env
from factorio_ai.persistence import PersistentRunState
from factorio_ai.plan_repair import repair_power_network_disconnect
from factorio_ai.planning import validate_factory_plan


CHECKPOINT_PATH = Path("state/active_factory_plan.json")


def _load_checkpoint() -> dict:
    try:
        value = json.loads(CHECKPOINT_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"No checkpoint found at {CHECKPOINT_PATH}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Checkpoint is not valid JSON: {CHECKPOINT_PATH}") from exc
    if not isinstance(value, dict) or not isinstance(value.get("plan"), dict):
        raise RuntimeError("Checkpoint does not contain a structured plan")
    return value


def _print_validation(prefix: str, validation: dict) -> None:
    print(
        f"{prefix}: {validation.get('status')} issues={validation.get('issue_count', '?')} "
        f"warnings={validation.get('warning_count', '?')}"
    )
    for issue in validation.get("issues", []) or []:
        if isinstance(issue, dict):
            print(f"  - {issue.get('code')}: {issue.get('message')}")


async def main() -> int:
    load_dotenv()
    settings = Settings.from_env(require_cloud=False)
    checkpoint = _load_checkpoint()
    checkpoint_player = str(checkpoint.get("player_name", ""))
    goal = str(checkpoint.get("goal", "")).strip()
    if not goal:
        raise RuntimeError("Checkpoint does not contain its original goal")
    if checkpoint_player and checkpoint_player != settings.player_name:
        raise RuntimeError(
            f"Checkpoint belongs to player {checkpoint_player!r}, current FACTORIO_PLAYER_NAME is {settings.player_name!r}"
        )
    if not settings.mcp_project.exists():
        raise FileNotFoundError(
            f"FactorioMCP project not found: {settings.mcp_project}. "
            "Run scripts/bootstrap-factorio-mcp.ps1 first."
        )

    store = PersistentRunState(Path("state"), settings.player_name, goal)
    attempt = int(checkpoint.get("validation_attempt") or 0)
    plan = checkpoint["plan"]

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
            tool_names = {tool.name for tool in listed.tools}

            async def call_mcp(name: str, arguments: dict) -> str:
                return await _call_tool(session, name, arguments, settings.tool_result_max_chars)

            attempt += 1
            validation = await validate_factory_plan(plan, call_mcp, tool_names)
            store.save_plan_checkpoint(attempt, plan, validation)
            _print_validation(f"validation attempt {attempt}", validation)
            if validation.get("valid") is True:
                print("checkpoint already passes live validation; no cloud model was called")
                return 0

            repaired = await repair_power_network_disconnect(plan, validation, call_mcp)
            if repaired is None:
                print("no supported deterministic repair applies; checkpoint left at latest validation result")
                return 2

            attempt += 1
            repaired_validation = await validate_factory_plan(repaired, call_mcp, tool_names)
            old_count = int(validation.get("issue_count") or 0)
            new_count = int(repaired_validation.get("issue_count") or 0)
            if repaired_validation.get("valid") is True or new_count < old_count:
                store.save_plan_checkpoint(attempt, repaired, repaired_validation)
            _print_validation(f"repair validation attempt {attempt}", repaired_validation)

            if repaired_validation.get("valid") is True:
                print("deterministic checkpoint repair PASSED; PLAN_VALID saved; no cloud model was called")
                return 0

            print("deterministic repair did not reach PLAN_VALID; no cloud model was called")
            return 2


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
