from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from openai import AsyncOpenAI

from .prompts import DANGEROUS_TOOL_NAMES, SYSTEM_PROMPT


BOOTSTRAP_CALLS: list[tuple[str, dict[str, Any]]] = [
    ("GetPlayerPosition", {}),
    ("GetInventory", {}),
    ("GetResearchStatus", {}),
    ("GetResearchedTechnologies", {}),
    ("GetAvailableTechnologies", {}),
    ("GetAvailableRecipes", {}),
    ("GetExistingFactorySummary", {}),
    ("GetBuildingSummary", {}),
    ("GetElectricNetwork", {}),
    ("GetNearbyEntities", {"radius": 40}),
]


@dataclass(frozen=True)
class Settings:
    provider: str
    player_name: str
    rcon_host: str
    rcon_port: str
    rcon_password: str
    mcp_project: Path
    model: str
    api_key: str
    base_url: str
    max_turns: int
    tool_result_max_chars: int

    @staticmethod
    def from_env(require_cloud: bool = True) -> "Settings":
        provider = os.getenv("AI_PROVIDER", "openai").strip().lower()
        if provider not in {"openai", "deepseek"}:
            raise ValueError("AI_PROVIDER must be 'openai' or 'deepseek'.")

        player_name = _required("FACTORIO_PLAYER_NAME")
        rcon_password = _required("FACTORIO_RCON_PASSWORD")
        mcp_project = Path(
            os.getenv(
                "FACTORIO_MCP_PROJECT",
                "src/FactorioMCP/FactorioMCP/FactorioMCP.csproj",
            )
        ).expanduser().resolve()

        if provider == "openai":
            api_key = os.getenv("OPENAI_API_KEY", "").strip()
            base_url = os.getenv("OPENAI_BASE_URL", "").strip().rstrip("/")
            model = os.getenv("OPENAI_MODEL", "gpt-5.6").strip()
            if require_cloud:
                if not api_key:
                    raise ValueError("OPENAI_API_KEY is required when AI_PROVIDER=openai.")
                if not base_url:
                    raise ValueError("OPENAI_BASE_URL is required when AI_PROVIDER=openai.")
        else:
            api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
            base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip().rstrip("/")
            model = os.getenv("DEEPSEEK_MODEL", "deepseek-flash").strip()
            if require_cloud and not api_key:
                raise ValueError("DEEPSEEK_API_KEY is required when AI_PROVIDER=deepseek.")

        return Settings(
            provider=provider,
            player_name=player_name,
            rcon_host=os.getenv("FACTORIO_RCON_HOST", "127.0.0.1").strip(),
            rcon_port=os.getenv("FACTORIO_RCON_PORT", "27015").strip(),
            rcon_password=rcon_password,
            mcp_project=mcp_project,
            model=model,
            api_key=api_key,
            base_url=base_url,
            max_turns=_positive_int("AGENT_MAX_TURNS", 80),
            tool_result_max_chars=_positive_int("AGENT_TOOL_RESULT_MAX_CHARS", 50_000),
        )


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required.")
    return value


def _positive_int(name: str, fallback: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return fallback
    value = int(raw)
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer.")
    return value


def _mcp_env(settings: Settings) -> dict[str, str]:
    return {
        "FACTORIO_PLAYER_NAME": settings.player_name,
        "FACTORIO_RCON_HOST": settings.rcon_host,
        "FACTORIO_RCON_PORT": settings.rcon_port,
        "FACTORIO_RCON_PASSWORD": settings.rcon_password,
        "FACTORIO_GOALS_FILE": str(Path("state/goals.json").resolve()),
        "FACTORIO_BUILDINGS_FILE": str(Path("state/buildings.json").resolve()),
    }


def _tool_schema(tool: Any) -> dict[str, Any]:
    schema = getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", None)
    if not isinstance(schema, dict):
        schema = {"type": "object", "properties": {}}
    return {
        "type": "function",
        "name": tool.name,
        "description": tool.description or "",
        "parameters": schema,
    }


def _dump_model(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(exclude_none=True)
    if isinstance(value, dict):
        return value
    raise TypeError(f"Cannot serialize response item of type {type(value)!r}")


def _tool_result_text(result: Any, max_chars: int) -> str:
    parts: list[str] = []
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        if isinstance(text, str):
            parts.append(text)
        elif hasattr(item, "model_dump_json"):
            parts.append(item.model_dump_json(exclude_none=True))
        else:
            parts.append(str(item))

    output = "\n".join(parts).strip() or "(tool returned no text content)"
    is_error = bool(getattr(result, "isError", False) or getattr(result, "is_error", False))
    if is_error:
        output = "MCP_TOOL_ERROR:\n" + output
    if len(output) > max_chars:
        output = output[:max_chars] + f"\n...[truncated at {max_chars} chars]"
    return output


async def _call_tool(
    session: ClientSession,
    name: str,
    arguments: dict[str, Any],
    max_chars: int,
) -> str:
    try:
        result = await session.call_tool(name, arguments=arguments)
        return _tool_result_text(result, max_chars)
    except Exception as exc:  # Tool failures are fed back to the model instead of killing the run.
        return f"MCP_TOOL_EXCEPTION: {type(exc).__name__}: {exc}"


async def _bootstrap_existing_save(
    session: ClientSession,
    tool_names: set[str],
    max_chars: int,
) -> str:
    sections: list[str] = []
    for name, arguments in BOOTSTRAP_CALLS:
        if name not in tool_names:
            continue
        result = await _call_tool(session, name, arguments, max_chars)
        sections.append(f"### {name}\n{result}")

    if not sections:
        return "No bootstrap tools were available. Inspect the world manually with the available MCP tools before acting."

    return "\n\n".join(sections)


async def _execute_function_calls(
    session: ClientSession,
    response: Any,
    max_chars: int,
) -> list[dict[str, Any]]:
    outputs: list[dict[str, Any]] = []
    for item in response.output:
        if getattr(item, "type", None) != "function_call":
            continue

        try:
            arguments = json.loads(item.arguments or "{}")
            if not isinstance(arguments, dict):
                raise ValueError("function arguments must decode to a JSON object")
        except Exception as exc:
            tool_output = f"INVALID_TOOL_ARGUMENTS: {exc}; raw={item.arguments!r}"
        else:
            print(f"[tool] {item.name} {json.dumps(arguments, ensure_ascii=False)}", file=sys.stderr)
            tool_output = await _call_tool(session, item.name, arguments, max_chars)
            print(f"[tool] {item.name} -> {tool_output[:300]}", file=sys.stderr)

        outputs.append(
            {
                "type": "function_call_output",
                "call_id": item.call_id,
                "output": tool_output,
            }
        )
    return outputs


async def _run_openai_stateful(
    session: ClientSession,
    client: AsyncOpenAI,
    settings: Settings,
    goal: str,
    tools: list[dict[str, Any]],
    bootstrap: str,
) -> str:
    initial_input = f"USER GOAL:\n{goal}\n\nEXISTING SAVE BOOTSTRAP:\n{bootstrap}"
    response = await client.responses.create(
        model=settings.model,
        instructions=SYSTEM_PROMPT,
        input=initial_input,
        tools=tools,
    )

    for _ in range(settings.max_turns):
        outputs = await _execute_function_calls(session, response, settings.tool_result_max_chars)
        if not outputs:
            return response.output_text or "(model completed without text output)"

        response = await client.responses.create(
            model=settings.model,
            instructions=SYSTEM_PROMPT,
            previous_response_id=response.id,
            input=outputs,
            tools=tools,
        )

    raise RuntimeError(f"Agent exceeded AGENT_MAX_TURNS={settings.max_turns}.")


async def _run_deepseek_stateless(
    session: ClientSession,
    client: AsyncOpenAI,
    settings: Settings,
    goal: str,
    tools: list[dict[str, Any]],
    bootstrap: str,
) -> str:
    history: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": f"USER GOAL:\n{goal}\n\nEXISTING SAVE BOOTSTRAP:\n{bootstrap}",
        }
    ]

    for _ in range(settings.max_turns):
        response = await client.responses.create(
            model=settings.model,
            instructions=SYSTEM_PROMPT,
            input=history,
            tools=tools,
        )

        outputs = await _execute_function_calls(session, response, settings.tool_result_max_chars)
        if not outputs:
            return response.output_text or "(model completed without text output)"

        # DeepSeek Responses is stateless. Preserve assistant messages and function-call
        # items explicitly, then append the matching function_call_output items.
        for item in response.output:
            if getattr(item, "type", None) in {"message", "function_call"}:
                history.append(_dump_model(item))
        history.extend(outputs)

    raise RuntimeError(f"Agent exceeded AGENT_MAX_TURNS={settings.max_turns}.")


async def run(goal: str, bootstrap_only: bool = False, list_tools: bool = False) -> int:
    load_dotenv()
    settings = Settings.from_env(require_cloud=not (bootstrap_only or list_tools))

    if not settings.mcp_project.exists():
        raise FileNotFoundError(
            f"FactorioMCP project not found: {settings.mcp_project}. "
            "Run scripts/bootstrap-factorio-mcp.ps1 first."
        )

    Path("state").mkdir(parents=True, exist_ok=True)

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
            visible_tools = [tool for tool in listed.tools if tool.name not in DANGEROUS_TOOL_NAMES]
            tool_names = {tool.name for tool in visible_tools}

            if list_tools:
                for name in sorted(tool_names):
                    print(name)
                return 0

            bootstrap = await _bootstrap_existing_save(
                session,
                tool_names,
                settings.tool_result_max_chars,
            )

            if bootstrap_only:
                print(bootstrap)
                return 0

            tools = [_tool_schema(tool) for tool in visible_tools]
            client = AsyncOpenAI(api_key=settings.api_key, base_url=settings.base_url)

            if settings.provider == "openai":
                final_text = await _run_openai_stateful(
                    session, client, settings, goal, tools, bootstrap
                )
            else:
                final_text = await _run_deepseek_stateless(
                    session, client, settings, goal, tools, bootstrap
                )

            print(final_text)
            return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Cloud AI orchestrator for FactorioMCP")
    parser.add_argument("goal", nargs="?", default="Inspect the existing save and report its current state.")
    parser.add_argument(
        "--bootstrap-only",
        action="store_true",
        help="Connect to FactorioMCP, print existing-save bootstrap state, and do not call a cloud model.",
    )
    parser.add_argument(
        "--list-tools",
        action="store_true",
        help="List the MCP tools exposed to the cloud model and exit.",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    try:
        raise SystemExit(asyncio.run(run(args.goal, args.bootstrap_only, args.list_tools)))
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
