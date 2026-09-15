# Safety model

The project intentionally treats the cloud model as untrusted orchestration logic. It receives a curated MCP tool set, not unrestricted Factorio console access.

## Player targeting

`FACTORIO_PLAYER_NAME` is mandatory. FactorioMCP is patched so player lookups use the configured name rather than `game.connected_players[1]`.

At MCP startup the configured player must:

- exist in the save; and
- be connected right now.

If either condition fails, FactorioMCP startup fails instead of silently choosing another multiplayer character.

## Raw Lua

Upstream FactorioMCP exposes `ExecuteLua`, which can execute arbitrary Lua on the Factorio server. The orchestrator removes this tool before sending the function-tool schema to OpenAI or DeepSeek.

`ClearBuildingMemory` is also hidden from the cloud model by default because it destroys persistent AI bookkeeping.

The local MCP implementation still uses internal Lua to implement bounded tools; the cloud model cannot submit arbitrary Lua source.

## RCON exposure

For the GUI-hosted Windows setup, bind local RCON to `127.0.0.1`. Do not expose the RCON port on the Radmin VPN adapter or a public interface.

The Cloudflare Worker and cloud model never receive the RCON password and cannot connect to Factorio directly.

## Cleanup / emergency stop

FactorioMCP's movement and resource mining use state persisted in Factorio `storage` so an `on_tick` handler can re-apply player state every tick.

The factorio-ai patch adds `EmergencyStop`, which:

- clears `storage.walk_dir`;
- clears `storage.mine_state`;
- sets `walking_state` to stopped;
- sets `mining_state` to stopped.

The orchestrator calls `EmergencyStop` in a `finally` block whenever a cloud-agent run ends normally or with an ordinary error.

A hard process kill, OS crash, or power loss can bypass application cleanup. If that happens, reconnect and invoke the orchestrator again or use a local RCON command to clear the persisted state before resuming normal play.

## Other players

The AI is instructed not to interfere with other player characters or belongings. This is a policy guard in addition to the hard named-player targeting. World-level tools can still modify shared factory infrastructure because Factorio multiplayer factories are shared by design; use the agent only in a server where the other players accept AI automation.
