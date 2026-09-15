using FactorioMCP.Rcon;

namespace FactorioMCP.Services;

/// <summary>
/// Initializes the single Factorio player controlled by this MCP process.
/// The selected name is stored in Factorio's persistent `storage` table so all
/// Lua snippets can resolve the same player without relying on connection order.
/// </summary>
internal sealed class FactorioPlayerTarget(RconClient rcon)
{
    public async Task<string> InitializeAsync(
        string playerName,
        CancellationToken cancellationToken = default)
    {
        if (string.IsNullOrWhiteSpace(playerName))
        {
            throw new InvalidOperationException(
                "FACTORIO_PLAYER_NAME is required. Refusing to fall back to connected_players[1] in multiplayer.");
        }

        var escapedName = EscapeLuaString(playerName.Trim());
        var lua = $$"""
            storage.factorio_mcp_player_name = "{{escapedName}}"
            local p = game.get_player(storage.factorio_mcp_player_name)
            if not p then
                error("Configured FACTORIO_PLAYER_NAME was not found: " .. storage.factorio_mcp_player_name)
            end
            if not p.connected then
                error("Configured FACTORIO_PLAYER_NAME is not currently connected: " .. storage.factorio_mcp_player_name)
            end
            rcon.print('{"name":"' .. p.name .. '","connected":true}')
            """;

        return await rcon.ExecuteLuaAsync(lua, cancellationToken);
    }

    private static string EscapeLuaString(string value) => value
        .Replace("\\", "\\\\", StringComparison.Ordinal)
        .Replace("\"", "\\\"", StringComparison.Ordinal)
        .Replace("\r", "\\r", StringComparison.Ordinal)
        .Replace("\n", "\\n", StringComparison.Ordinal)
        .Replace("\t", "\\t", StringComparison.Ordinal);
}
