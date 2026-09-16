# Windows GUI host setup

This project does **not** require a dedicated/headless Factorio server.

You can keep the existing flow:

1. start normal Factorio;
2. `Multiplayer -> Host saved game`;
3. choose the same save you already use;
4. friends connect to your Radmin VPN IP as before.

The extra requirement is local RCON for the Factorio instance that hosts the multiplayer game.

## Enable local RCON

In Factorio's `config.ini`, under `[other]`, configure:

```ini
local-rcon-socket=127.0.0.1:27015
local-rcon-password=CHANGE_THIS_PASSWORD
```

If the existing lines start with `;`, remove the `;` because it comments the setting out.

`127.0.0.1` deliberately exposes RCON only on the host PC. Do not bind the RCON socket to the Radmin adapter or a public interface.

After changing the config:

1. fully close Factorio;
2. start Factorio again;
3. host the same save through the multiplayer menu.

You do not need to create a new save. Friends do not change how they connect and do not install mods.

## Match `.env`

```dotenv
FACTORIO_RCON_HOST=127.0.0.1
FACTORIO_RCON_PORT=27015
FACTORIO_RCON_PASSWORD=CHANGE_THIS_PASSWORD
FACTORIO_PLAYER_NAME=YourExactFactorioName
```

`FACTORIO_PLAYER_NAME` is the one character the AI is allowed to target. The patched FactorioMCP refuses to start without it instead of falling back to the first connected player.

## First test

Before using any cloud model, validate the complete Factorio -> RCON -> MCP path:

```powershell
factorio-ai --bootstrap-only
```

Expected output contains sections such as:

- `GetPlayerPosition`
- `GetInventory`
- `GetResearchStatus`
- `GetResearchedTechnologies`
- `GetAvailableRecipes`
- `GetExistingFactorySummary`
- `GetNearbyEntities`

If this works, the AI has enough initial context to enter an already-developed save instead of behaving as if the map were new.

## Achievements

FactorioMCP uses scripting commands over RCON. Once scripting commands are used in a save, Factorio achievements for that save are disabled. This project assumes that tradeoff is acceptable.
