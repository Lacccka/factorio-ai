# factorio-ai

Cloud-hosted AI control stack for a **vanilla Factorio multiplayer server**. Other players connect normally and do not install any mod.

The project uses [FactorioMCP](https://github.com/sbarisic/FactorioMCP) as the game-control layer and adds:

- deterministic targeting of one Factorio player by `FACTORIO_PLAYER_NAME` instead of `game.connected_players[1]`;
- a cloud-only orchestrator (no local LLM);
- OpenAI through a user-owned Cloudflare Worker;
- DeepSeek directly through its API;
- a startup prompt that assumes the save may already contain research, machines, logistics and other player-built infrastructure;
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
   +--> Cloudflare Worker --> OpenAI Responses API
```

There is no local inference. The Python process only orchestrates cloud model calls and MCP tool calls.

## Current status

This branch bootstraps the first vertical slice. The repository intentionally pins an upstream FactorioMCP commit and applies a small multiplayer-safety patch during setup instead of permanently copying the whole upstream tree. That keeps future upstream updates reviewable.

## Prerequisites

- Windows (the initial setup script is PowerShell-first)
- Git
- .NET 9 SDK
- Python 3.11+
- Factorio 2.x with local RCON enabled
- either:
  - OpenAI API access behind your Cloudflare Worker, or
  - a DeepSeek API key

## 1. Configure Factorio

Keep hosting the game normally from the Factorio UI. Friends can keep connecting through Radmin VPN as before. The only additional requirement is a local RCON endpoint bound to your machine.

Use the same password in Factorio and `.env`.

## 2. Bootstrap FactorioMCP

From PowerShell in the repository root:

```powershell
./scripts/bootstrap-factorio-mcp.ps1
```

The script clones the pinned upstream FactorioMCP revision into `src/FactorioMCP`, patches multiplayer player selection, and builds it.

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

### OpenAI through Cloudflare Worker

```dotenv
AI_PROVIDER=openai
OPENAI_BASE_URL=https://your-worker.workers.dev/v1
OPENAI_API_KEY=your-worker-shared-secret
OPENAI_MODEL=gpt-5.6
```

The value stored on the gaming PC is the Worker shared secret, not the real OpenAI key. The real OpenAI key stays in Cloudflare.

### DeepSeek directly

```dotenv
AI_PROVIDER=deepseek
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_API_KEY=your-real-deepseek-key
DEEPSEEK_MODEL=deepseek-flash
```

DeepSeek does not pass through the Worker.

## 4. Install the orchestrator

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ./src/orchestrator
```

## 5. Run

```powershell
factorio-ai "Inspect the existing factory, then increase green-circuit production without rebuilding systems that already work."
```

On startup the agent is instructed to inspect the existing save first: player state, nearby entities, research state, unlocked recipes, tracked buildings and power state. It must not assume a fresh game.

## Cloudflare Worker

`worker/worker.js` is intended for the Cloudflare dashboard editor: paste the file into **Workers & Pages -> Edit code**. Configure two secrets in the dashboard:

- `OPENAI_API_KEY` — the real OpenAI API key;
- `WORKER_SHARED_SECRET` — a long random secret used by the local orchestrator.

The Worker only proxies `POST /v1/responses`.

## Upstream attribution

FactorioMCP is Copyright its original contributors and is licensed under the MIT License. See `THIRD_PARTY_NOTICES.md`. The bootstrap script pins the upstream commit so changes are reproducible.
