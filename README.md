# factorio-ai

Cloud-hosted AI control stack for a **vanilla Factorio multiplayer server**. Other players connect normally and do not install any mod.

The project uses [FactorioMCP](https://github.com/sbarisic/FactorioMCP) as the game-control layer and adds:

- deterministic targeting of one Factorio player by `FACTORIO_PLAYER_NAME` instead of `game.connected_players[1]`;
- a cloud-only orchestrator (no local LLM);
- OpenAI Responses API either directly or through a user-owned Cloudflare Worker;
- DeepSeek directly through its API;
- a startup prompt that assumes the save may already contain research, machines, logistics and other player-built infrastructure;
- hard mutation/failure budgets to stop tool thrashing;
- optional plan-gated factory expansion: mutating tools stay hidden until a structured plan passes deterministic validation;
- persistent factory knowledge and exact-goal plan checkpoints across CLI restarts;
- per-run tool/token/timing metrics;
- dangerous raw Lua hidden from the cloud model by default.

## Architecture

```text
Factorio (vanilla multiplayer)
        ^
        | localhost RCON
        v
FactorioMCP (patched at setup)
        ^
        | MCP stdio
        v
Python orchestrator
   |             |
   |             +--> DeepSeek API directly
   |
   +--> OpenAI Responses API directly
   |
   +--> Cloudflare Worker --> OpenAI Responses API
```

There is no local inference. The Python process only orchestrates cloud model calls and MCP tool calls.

## Current status

This branch bootstraps the first vertical slice. The repository intentionally pins an upstream FactorioMCP commit and applies a small multiplayer-safety/compatibility patch during setup instead of permanently copying the whole upstream tree. That keeps future upstream updates reviewable.

## Prerequisites

- Windows (the initial setup script is PowerShell-first)
- Git
- .NET 9 SDK
- Python 3.11+
- Factorio 2.x with local RCON enabled
- either:
  - direct OpenAI API access;
  - OpenAI API access behind your Cloudflare Worker; or
  - a DeepSeek API key

## 1. Configure Factorio

Keep hosting the game normally from the Factorio UI. Friends can keep connecting through Radmin VPN as before. The only additional requirement is a local RCON endpoint bound to your machine.

Use the same password in Factorio and `.env`.

## 2. Bootstrap FactorioMCP

From PowerShell in the repository root:

```powershell
./scripts/bootstrap-factorio-mcp.ps1
```

The script clones the pinned upstream FactorioMCP revision into `src/FactorioMCP`, patches multiplayer player selection, applies Factorio 2.0 compatibility/safety tools, and builds it.

## 3. Configure environment

Copy:

```powershell
Copy-Item .env.example .env
```

At minimum set:

```dotenv
FACTORIO_PLAYER_NAME=YourExactFactorioName
FACTORIO_RCON_PASSWORD=your-rcon-password
```

Then choose one cloud provider.

### OpenAI directly

```dotenv
AI_PROVIDER=openai
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_API_KEY=your-real-openai-api-key
OPENAI_MODEL=gpt-5.6-terra
```

### OpenAI through Cloudflare Worker

```dotenv
AI_PROVIDER=openai
OPENAI_BASE_URL=https://your-worker.workers.dev/v1
OPENAI_API_KEY=your-worker-shared-secret
OPENAI_MODEL=gpt-5.6-terra
```

When using the Worker, the value stored on the gaming PC is the Worker shared secret, not the real OpenAI key. The real OpenAI key stays in Cloudflare.

### DeepSeek directly

```dotenv
AI_PROVIDER=deepseek
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_API_KEY=your-real-deepseek-key
DEEPSEEK_MODEL=deepseek-flash
```

DeepSeek does not pass through the Worker.

### Agent guardrails

```dotenv
AGENT_MAX_TURNS=80
AGENT_MAX_MUTATIONS=80
AGENT_MAX_FAILED_MUTATIONS=8
AGENT_TOOL_RESULT_MAX_CHARS=50000
```

`AGENT_MAX_MUTATIONS` is a hard cap on calls that modify the world, inventory, or research state. Read-only inspection and ordinary walking do not consume it. `AGENT_MAX_FAILED_MUTATIONS` stops further mutations early when repeated build/mine/transfer attempts fail. Once either mutation budget is exhausted, the orchestrator exposes only read-only tools for the rest of the task so the model can verify state and report the blocker instead of thrashing.

At the end of a cloud run the CLI prints metrics including elapsed time, model turns, tool calls, mutation calls, failed mutations, plan-validation attempts/status, and API token usage when the provider returns usage data.

## 4. Install the orchestrator

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ./src/orchestrator
```

Re-run the editable install after pulling a revision that changes the console-script entry point or package metadata:

```powershell
pip install -e ./src/orchestrator
```

## 5. Run

```powershell
factorio-ai "Inspect the existing factory, then increase green-circuit production without rebuilding systems that already work."
```

On startup the agent inspects a compact existing-save snapshot and can query further state on demand. It must not assume a fresh game. Mutation tasks are diagnosis-first: the system prompt instructs the model to establish the relevant state/plan before crafting, mining, placing, or transferring items.

### Plan-gated factory expansion

For substantial new production lines, use `--plan-gated`:

```powershell
factorio-ai --plan-gated "Extend the existing factory with an automated defense production hub. Reuse the main bus and established assembler district, validate the complete design, then build only after the plan passes validation."
```

In this mode the model initially receives only read-only/navigation tools plus the orchestrator-local `submit_factory_plan` tool. World-changing tools are not exposed until `submit_factory_plan` returns `PLAN_VALID`.

A submitted plan is machine-checked against live Factorio state and recipe/prototype data. Validation currently checks:

- actual recipe times, machine crafting speed, machine counts and target production rates;
- required item rates against yellow/red/blue belt capacity and half-belt lane capacity;
- source-to-sink belt segment continuity and direction;
- claimed extension direction against an existing source belt when `source_mode=extend`;
- planned entity footprint overlap;
- live `surface.can_place_entity` feasibility without creating ghosts or entities;
- inserter pickup/drop geometry through explicit `pickup_ref`/`drop_ref` references;
- electric-pole supply coverage and connectivity back to a declared existing pole anchor.

If validation returns `PLAN_INVALID`, mutating tools remain hidden and the model must correct the reported issues and resubmit. This mode is intended for large build/expansion tasks; small repairs can continue using the normal CLI path.

To verify the patched read-only preflight tool after bootstrap:

```powershell
factorio-ai --list-tools | Select-String "survey_factory_layout|check_entity_placement_batch"
```

### Persistent knowledge and plan resume

The orchestrator keeps two local, git-ignored checkpoint files under `state/`:

```text
state/factory_knowledge.json
state/active_factory_plan.json
```

`factory_knowledge.json` stores bounded successful observations from architecture-oriented read-only tools such as `survey_factory_layout`, `scan_resources`, `get_power_network_topology`, `find_buildable_area`, and `summarize_area`. On a later run for the same configured Factorio player, these observations are injected as cached knowledge so the model can reuse an already-discovered main bus, assembler district, resource area and power layout instead of repeating broad surveys. Successful world mutations are timestamped so older observations are treated as potentially stale and only affected local facts should be revalidated.

`active_factory_plan.json` stores the latest structured plan, validation result and up to the most recent execution mutation steps. A checkpoint is automatically resumed only when the normalized user goal is exactly the same and the configured Factorio player matches. `PLAN_INVALID` runs therefore resume from the last plan and remaining issues rather than planning from zero. A plan that was `PLAN_VALID` in a previous process is still re-submitted for fresh live validation before mutating tools are unlocked, because another player or a prior partial execution may have changed the world.

These files deliberately remain local and are excluded by `state/.gitignore`; they may contain detailed coordinates and local factory state.

## Cloudflare Worker

`worker/worker.js` is intended for the Cloudflare dashboard editor: paste the file into **Workers & Pages -> Edit code**. Configure two secrets in the dashboard:

- `OPENAI_API_KEY` — the real OpenAI API key;
- `WORKER_SHARED_SECRET` — a long random secret used by the local orchestrator.

The Worker only proxies `POST /v1/responses`.

## Upstream attribution

FactorioMCP is Copyright its original contributors and is licensed under the MIT License. See `THIRD_PARTY_NOTICES.md`. The bootstrap script pins the upstream commit so changes are reproducible.
