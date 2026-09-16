using FactorioMCP.Services;
using ModelContextProtocol.Server;
using System.ComponentModel;
using System.Globalization;

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
        "Remove only non-interactive Factorio '*-remnants' corpse entities near an exact position when they block rebuilding destroyed infrastructure. " +
        "Use only after inspection or occupancy confirms remnants are the blocker. This never mines or removes live buildings, resources, ghosts, or items.")]
    public Task<string> ClearRemnants(
        double x,
        double y,
        double radius = 1.5,
        CancellationToken cancellationToken = default)
    {
        if (radius <= 0 || radius > 3)
        {
            throw new ArgumentOutOfRangeException(nameof(radius), "radius must be > 0 and <= 3 tiles.");
        }

        var lua = string.Create(CultureInfo.InvariantCulture, $$"""
            local function esc(s) return s:gsub('\\', '\\\\'):gsub('"', '\\"') end
            local p = game.get_player(storage.factorio_mcp_player_name)
            if not p then error("Configured Factorio player does not exist") end

            local target = {x={{x}}, y={{y}}}
            local center_dx = target.x - p.position.x
            local center_dy = target.y - p.position.y
            local center_distance = math.sqrt(center_dx * center_dx + center_dy * center_dy)
            if center_distance > p.reach_distance + {{radius}} then
                rcon.print('{"success":false,"error":"out_of_range","distance":' .. string.format("%.1f", center_distance) .. ',"limit":' .. p.reach_distance .. '}')
                return
            end

            local removed = {}
            local corpses = p.surface.find_entities_filtered{
                position=target,
                radius={{radius}},
                type='corpse'
            }

            for _, entity in ipairs(corpses) do
                if entity.valid and string.sub(entity.name, -9) == '-remnants' then
                    local dx = entity.position.x - p.position.x
                    local dy = entity.position.y - p.position.y
                    local distance = math.sqrt(dx * dx + dy * dy)
                    if distance <= p.reach_distance then
                        local name = entity.name
                        local ex = entity.position.x
                        local ey = entity.position.y
                        local destroyed = entity.destroy()
                        if destroyed ~= false then
                            removed[#removed + 1] = '{"name":"' .. esc(name) .. '","x":' .. ex .. ',"y":' .. ey .. '}'
                        end
                    end
                end
            end

            rcon.print('{"success":true,"removed_count":' .. #removed .. ',"removed":[' .. table.concat(removed, ',') .. ']}')
            """);

        return queue.ExecuteAsync(nameof(ClearRemnants), ct => factorio.ExecuteRawLuaAsync(lua, ct), cancellationToken);
    }

    [McpServerTool, Description(
        "Assign an enabled recipe to a nearby blank assembling machine using normal Factorio recipe mechanics. " +
        "This is intended for freshly placed assemblers. It is idempotent for the same recipe and refuses to overwrite a different existing recipe.")]
    public Task<string> SetAssemblerRecipe(
        double x,
        double y,
        string recipe,
        CancellationToken cancellationToken = default)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(recipe);
        var escapedRecipe = recipe.Replace("\\", "\\\\").Replace("\"", "\\\"");

        var lua = string.Create(CultureInfo.InvariantCulture, $$"""
            local function esc(s) return s:gsub('\\', '\\\\'):gsub('"', '\\"') end
            local p = game.get_player(storage.factorio_mcp_player_name)
            if not p then error("Configured Factorio player does not exist") end

            local target = {x={{x}}, y={{y}}}
            local dx = target.x - p.position.x
            local dy = target.y - p.position.y
            local distance = math.sqrt(dx * dx + dy * dy)
            if distance > p.reach_distance then
                rcon.print('{"success":false,"error":"out_of_range","distance":' .. string.format("%.1f", distance) .. ',"limit":' .. p.reach_distance .. '}')
                return
            end

            local machines = p.surface.find_entities_filtered{
                position=target,
                radius=0.75,
                type='assembling-machine'
            }
            local e = nil
            local best = nil
            for _, candidate in ipairs(machines) do
                if candidate.valid then
                    local cdx = candidate.position.x - target.x
                    local cdy = candidate.position.y - target.y
                    local d2 = cdx * cdx + cdy * cdy
                    if best == nil or d2 < best then
                        e = candidate
                        best = d2
                    end
                end
            end
            if not e then
                rcon.print('{"success":false,"error":"no_assembling_machine","x":' .. target.x .. ',"y":' .. target.y .. '}')
                return
            end

            local recipe_name = "{{escapedRecipe}}"
            local current = e.get_recipe()
            if current then
                if current.name == recipe_name then
                    rcon.print('{"success":true,"status":"already_set","entity":"' .. esc(e.name) .. '","recipe":"' .. esc(recipe_name) .. '","x":' .. e.position.x .. ',"y":' .. e.position.y .. '}')
                else
                    rcon.print('{"success":false,"error":"recipe_already_set","entity":"' .. esc(e.name) .. '","current_recipe":"' .. esc(current.name) .. '","requested_recipe":"' .. esc(recipe_name) .. '"}')
                end
                return
            end

            local force_recipe = p.force.recipes[recipe_name]
            if not force_recipe then
                rcon.print('{"success":false,"error":"unknown_recipe","recipe":"' .. esc(recipe_name) .. '"}')
                return
            end
            if not force_recipe.enabled then
                rcon.print('{"success":false,"error":"recipe_locked","recipe":"' .. esc(recipe_name) .. '"}')
                return
            end

            local ok, result = pcall(function() return e.set_recipe(recipe_name) end)
            if not ok then
                rcon.print('{"success":false,"error":"set_recipe_failed","entity":"' .. esc(e.name) .. '","recipe":"' .. esc(recipe_name) .. '","message":"' .. esc(tostring(result)) .. '"}')
                return
            end

            local actual = e.get_recipe()
            if not actual or actual.name ~= recipe_name then
                rcon.print('{"success":false,"error":"recipe_not_applied","entity":"' .. esc(e.name) .. '","recipe":"' .. esc(recipe_name) .. '"}')
                return
            end

            rcon.print('{"success":true,"status":"set","entity":"' .. esc(e.name) .. '","recipe":"' .. esc(actual.name) .. '","x":' .. e.position.x .. ',"y":' .. e.position.y .. '}')
            """);

        return queue.ExecuteAsync(nameof(SetAssemblerRecipe), ct => factorio.ExecuteRawLuaAsync(lua, ct), cancellationToken);
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
