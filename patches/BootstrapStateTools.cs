using FactorioMCP.Services;
using ModelContextProtocol.Server;
using System.ComponentModel;

namespace FactorioMCP.Tools;

/// <summary>
/// Extra tools used by factorio-ai to bootstrap an agent into an existing save
/// and to leave the selected player in a safe idle state when the orchestrator exits.
/// </summary>
[McpServerToolType]
internal sealed class BootstrapStateTools(FactorioService factorio, GameCommandQueue queue)
{
    [McpServerTool, Description(
        "List every technology already researched by the configured player's force. " +
        "Use this when joining an existing save so you do not assume early-game progression.")]
    public Task<string> GetResearchedTechnologies(CancellationToken cancellationToken = default)
    {
        return queue.ExecuteAsync(nameof(GetResearchedTechnologies), ct => factorio.ExecuteRawLuaAsync("""
            local p = game.get_player(storage.factorio_mcp_player_name)
            if not p then error("Configured Factorio player does not exist") end
            local names = {}
            for name, tech in pairs(p.force.technologies) do
                if tech.researched then names[#names + 1] = name end
            end
            table.sort(names)
            local out = {}
            for _, name in ipairs(names) do out[#out + 1] = '"' .. name .. '"' end
            rcon.print('{"researched":[' .. table.concat(out, ',') .. ']}')
            """, ct), cancellationToken);
    }

    [McpServerTool, Description(
        "Summarize all existing entities owned by the configured player's force, grouped by surface and prototype name. " +
        "This sees factory infrastructure that existed before AI building-memory tracking began.")]
    public Task<string> GetExistingFactorySummary(CancellationToken cancellationToken = default)
    {
        return queue.ExecuteAsync(nameof(GetExistingFactorySummary), ct => factorio.ExecuteRawLuaAsync("""
            local function esc(s) return s:gsub('\\', '\\\\'):gsub('"', '\\"') end
            local p = game.get_player(storage.factorio_mcp_player_name)
            if not p then error("Configured Factorio player does not exist") end
            local surfaces = {}
            for _, surface in pairs(game.surfaces) do
                local counts = {}
                local entities = surface.find_entities_filtered{force = p.force}
                for _, entity in ipairs(entities) do
                    if entity.valid and entity.type ~= 'character' and entity.type ~= 'entity-ghost' then
                        counts[entity.name] = (counts[entity.name] or 0) + 1
                    end
                end
                local names = {}
                for name, _ in pairs(counts) do names[#names + 1] = name end
                table.sort(names)
                local countParts = {}
                for _, name in ipairs(names) do
                    countParts[#countParts + 1] = '"' .. name .. '":' .. counts[name]
                end
                surfaces[#surfaces + 1] = '{"surface":"' .. esc(surface.name) .. '","counts":{' .. table.concat(countParts, ',') .. '}}'
            end
            rcon.print('{"surfaces":[' .. table.concat(surfaces, ',') .. ']}')
            """, ct), cancellationToken);
    }

    [McpServerTool, Description(
        "Immediately stop walking and active mining for the configured player and clear FactorioMCP's persisted movement/mining state. " +
        "Safe to call repeatedly; intended as an emergency/cleanup stop.")]
    public Task<string> EmergencyStop(CancellationToken cancellationToken = default)
    {
        return queue.ExecuteAsync(nameof(EmergencyStop), ct => factorio.ExecuteRawLuaAsync("""
            local p = game.get_player(storage.factorio_mcp_player_name)
            if not p then error("Configured Factorio player does not exist") end
            storage.walk_dir = nil
            storage.mine_state = nil
            p.walking_state = {walking = false}
            p.mining_state = {mining = false}
            rcon.print('{"success":true,"status":"stopped"}')
            """, ct), cancellationToken);
    }
}
