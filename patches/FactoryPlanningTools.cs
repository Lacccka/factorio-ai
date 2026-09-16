using FactorioMCP.Services;
using ModelContextProtocol.Server;
using System.ComponentModel;
using System.Globalization;
using System.Text;
using System.Text.Json;

namespace FactorioMCP.Tools;

/// <summary>
/// Read-only planning helpers for understanding and validating an existing factory layout before building.
/// </summary>
[McpServerToolType]
internal sealed class FactoryPlanningTools(FactorioService factorio, GameCommandQueue queue)
{
    [McpServerTool, Description(
        "Survey an existing factory area for main-bus style transport-belt runs and assembler zones. " +
        "Returns long straight belt runs grouped by axis/direction, representative item contents from the belt lanes, " +
        "and dense assembling-machine zones. Use this before designing a new production extension so existing bus lanes " +
        "and the established assembler district are reused instead of creating ad-hoc chest/manual logistics.")]
    public Task<string> SurveyFactoryLayout(
        [Description("Search radius in tiles. Default 60.")]
        double radius = 60,
        [Description("Optional X coordinate of scan center. If omitted, uses configured player position.")]
        double? centerX = null,
        [Description("Optional Y coordinate of scan center. If omitted, uses configured player position.")]
        double? centerY = null,
        [Description("Minimum number of contiguous belt tiles for a run to be reported. Default 4.")]
        int minRunLength = 4,
        CancellationToken cancellationToken = default)
    {
        if (radius <= 0 || radius > 250)
            throw new ArgumentOutOfRangeException(nameof(radius), "radius must be > 0 and <= 250 tiles.");
        if (minRunLength < 2 || minRunLength > 100)
            throw new ArgumentOutOfRangeException(nameof(minRunLength), "minRunLength must be between 2 and 100.");

        var centerExpr = centerX.HasValue && centerY.HasValue
            ? string.Create(CultureInfo.InvariantCulture, $"{{x={centerX.Value},y={centerY.Value}}}")
            : "p.position";

        var lua = string.Create(CultureInfo.InvariantCulture, $$$"""
            local function esc(s) return s:gsub('\\', '\\\\'):gsub('"', '\\"') end
            local p = game.get_player(storage.factorio_mcp_player_name)
            if not p then error("Configured Factorio player does not exist") end
            local surface = p.surface
            local center = {{{centerExpr}}}
            local radius = {{{radius}}}
            local min_run = {{{minRunLength}}}

            local dir_names = {}
            for k, v in pairs(defines.direction) do dir_names[v] = k end

            -- Gather ordinary belt tiles and group them by straight line + flow direction.
            local belts = surface.find_entities_filtered{
                position=center,
                radius=radius,
                type="transport-belt",
                force=p.force
            }
            local groups = {}
            for _, e in pairs(belts) do
                if e.valid then
                    local horizontal = e.direction == defines.direction.east or e.direction == defines.direction.west
                    local vertical = e.direction == defines.direction.north or e.direction == defines.direction.south
                    if horizontal or vertical then
                        local fixed = horizontal and e.position.y or e.position.x
                        local variable = horizontal and e.position.x or e.position.y
                        local axis = horizontal and "horizontal" or "vertical"
                        local key = axis..":"..string.format("%.1f", fixed)..":"..tostring(e.direction)
                        if not groups[key] then
                            groups[key] = {axis=axis, fixed=fixed, direction=e.direction, entries={}}
                        end
                        groups[key].entries[#groups[key].entries+1] = {coord=variable, entity=e}
                    end
                end
            end

            local function sample_items(e)
                local counts = {}
                local ok_max, max_line = pcall(function() return e.get_max_transport_line_index() end)
                if not ok_max or not max_line then return counts end
                for i = 1, max_line do
                    local ok_line, line = pcall(function() return e.get_transport_line(i) end)
                    if ok_line and line and line.valid then
                        local ok_contents, contents = pcall(function() return line.get_contents() end)
                        if ok_contents and contents then
                            for _, item in pairs(contents) do
                                if item.name and item.count then
                                    counts[item.name] = (counts[item.name] or 0) + item.count
                                end
                            end
                        end
                    end
                end
                return counts
            end

            local function items_json(counts)
                local arr = {}
                for name, count in pairs(counts) do
                    arr[#arr+1] = {name=name, count=count}
                end
                table.sort(arr, function(a,b)
                    if a.count ~= b.count then return a.count > b.count end
                    return a.name < b.name
                end)
                local out = {}
                for i = 1, math.min(#arr, 8) do
                    local v = arr[i]
                    out[#out+1] = '{"name":"'..esc(v.name)..'","count":'..v.count..'}'
                end
                return '['..table.concat(out, ',')..']'
            end

            local runs = {}
            for _, g in pairs(groups) do
                table.sort(g.entries, function(a,b) return a.coord < b.coord end)
                local start_i = 1
                while start_i <= #g.entries do
                    local end_i = start_i
                    while end_i < #g.entries and (g.entries[end_i+1].coord - g.entries[end_i].coord) <= 1.01 do
                        end_i = end_i + 1
                    end
                    local tile_count = end_i - start_i + 1
                    if tile_count >= min_run then
                        local sample_i = math.floor((start_i + end_i) / 2)
                        local sample = g.entries[sample_i].entity
                        runs[#runs+1] = {
                            axis=g.axis,
                            fixed=g.fixed,
                            direction=g.direction,
                            start_coord=g.entries[start_i].coord,
                            end_coord=g.entries[end_i].coord,
                            tile_count=tile_count,
                            sample=sample,
                            belt_name=sample.name
                        }
                    end
                    start_i = end_i + 1
                end
            end
            table.sort(runs, function(a,b)
                if a.tile_count ~= b.tile_count then return a.tile_count > b.tile_count end
                if a.axis ~= b.axis then return a.axis < b.axis end
                return a.fixed < b.fixed
            end)

            local run_parts = {}
            for i = 1, math.min(#runs, 40) do
                local r = runs[i]
                local sx = r.sample.position.x
                local sy = r.sample.position.y
                local contents = sample_items(r.sample)
                run_parts[#run_parts+1] = '{"axis":"'..r.axis..'","flow_direction":"'..(dir_names[r.direction] or tostring(r.direction))..'","fixed_coordinate":'..string.format("%.1f",r.fixed)..',"start_coordinate":'..string.format("%.1f",r.start_coord)..',"end_coordinate":'..string.format("%.1f",r.end_coord)..',"tile_count":'..r.tile_count..',"belt":"'..esc(r.belt_name)..'","sample_x":'..string.format("%.1f",sx)..',"sample_y":'..string.format("%.1f",sy)..',"sample_items":'..items_json(contents)..'}'
            end

            -- Report dense assembler cells as likely established manufacturing zones.
            local assemblers = surface.find_entities_filtered{
                position=center,
                radius=radius,
                type="assembling-machine",
                force=p.force
            }
            local cell_size = 12
            local cells = {}
            for _, e in pairs(assemblers) do
                if e.valid then
                    local cx = math.floor(e.position.x / cell_size)
                    local cy = math.floor(e.position.y / cell_size)
                    local key = cx..":"..cy
                    if not cells[key] then
                        cells[key] = {count=0,min_x=e.position.x,max_x=e.position.x,min_y=e.position.y,max_y=e.position.y,recipes={}}
                    end
                    local c = cells[key]
                    c.count = c.count + 1
                    c.min_x = math.min(c.min_x, e.position.x)
                    c.max_x = math.max(c.max_x, e.position.x)
                    c.min_y = math.min(c.min_y, e.position.y)
                    c.max_y = math.max(c.max_y, e.position.y)
                    local ok_recipe, recipe = pcall(function() return e.get_recipe() end)
                    local rn = ok_recipe and recipe and recipe.name or "(blank)"
                    c.recipes[rn] = (c.recipes[rn] or 0) + 1
                end
            end
            local zones = {}
            for _, c in pairs(cells) do zones[#zones+1] = c end
            table.sort(zones, function(a,b) return a.count > b.count end)

            local zone_parts = {}
            for i = 1, math.min(#zones, 12) do
                local z = zones[i]
                local recipe_arr = {}
                for name, count in pairs(z.recipes) do recipe_arr[#recipe_arr+1] = {name=name,count=count} end
                table.sort(recipe_arr, function(a,b)
                    if a.count ~= b.count then return a.count > b.count end
                    return a.name < b.name
                end)
                local recipe_parts = {}
                for j = 1, math.min(#recipe_arr, 8) do
                    recipe_parts[#recipe_parts+1] = '{"recipe":"'..esc(recipe_arr[j].name)..'","count":'..recipe_arr[j].count..'}'
                end
                zone_parts[#zone_parts+1] = '{"assembler_count":'..z.count..',"min_x":'..string.format("%.1f",z.min_x)..',"max_x":'..string.format("%.1f",z.max_x)..',"min_y":'..string.format("%.1f",z.min_y)..',"max_y":'..string.format("%.1f",z.max_y)..',"recipes":['..table.concat(recipe_parts, ',')..']}'
            end

            rcon.print('{"status":"ok","center_x":'..string.format("%.1f",center.x)..',"center_y":'..string.format("%.1f",center.y)..',"radius":'..string.format("%.1f",radius)..',"belt_entity_count":'..#belts..',"reported_runs":'..math.min(#runs,40)..',"belt_runs":['..table.concat(run_parts, ',')..'],"assembler_count":'..#assemblers..',"assembler_zones":['..table.concat(zone_parts, ',')..']}')
            """);

        return queue.ExecuteAsync(nameof(SurveyFactoryLayout), ct => factorio.ExecuteRawLuaAsync(lua, ct), cancellationToken);
    }

    [McpServerTool, Description(
        "Read-only batch preflight for planned entity placements. Uses Factorio surface.can_place_entity without creating ghosts or entities. " +
        "Input is a JSON array of {id,entity_name,x,y,direction}. Returns prototype existence and can_place for every entry. " +
        "Intended for deterministic validation before an autonomous agent is allowed to mutate the world.")]
    public Task<string> CheckEntityPlacementBatch(
        [Description("JSON array of planned placements: [{\"id\":\"asm-1\",\"entity_name\":\"assembling-machine-2\",\"x\":3.5,\"y\":-196.5,\"direction\":\"north\"}]")]
        string placementsJson,
        CancellationToken cancellationToken = default)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(placementsJson);

        using var document = JsonDocument.Parse(placementsJson);
        if (document.RootElement.ValueKind != JsonValueKind.Array)
            throw new ArgumentException("placementsJson must be a JSON array.", nameof(placementsJson));
        if (document.RootElement.GetArrayLength() > 500)
            throw new ArgumentOutOfRangeException(nameof(placementsJson), "At most 500 placements can be checked at once.");

        static string LuaEscape(string value) => value.Replace("\\", "\\\\").Replace("\"", "\\\"");
        var entries = new StringBuilder();
        var allowedDirections = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
        {
            "north", "northeast", "east", "southeast", "south", "southwest", "west", "northwest"
        };

        foreach (var element in document.RootElement.EnumerateArray())
        {
            if (element.ValueKind != JsonValueKind.Object)
                throw new ArgumentException("Every placement must be a JSON object.", nameof(placementsJson));

            var id = element.TryGetProperty("id", out var idElement) ? idElement.GetString() ?? "" : "";
            var entityName = element.TryGetProperty("entity_name", out var nameElement) ? nameElement.GetString() ?? "" : "";
            if (string.IsNullOrWhiteSpace(id) || string.IsNullOrWhiteSpace(entityName))
                throw new ArgumentException("Every placement requires non-empty id and entity_name.", nameof(placementsJson));
            if (!element.TryGetProperty("x", out var xElement) || !xElement.TryGetDouble(out var x) ||
                !element.TryGetProperty("y", out var yElement) || !yElement.TryGetDouble(out var y))
                throw new ArgumentException($"Placement '{id}' requires numeric x and y.", nameof(placementsJson));

            var direction = element.TryGetProperty("direction", out var directionElement)
                ? directionElement.GetString() ?? "north"
                : "north";
            if (!allowedDirections.Contains(direction))
                throw new ArgumentException($"Placement '{id}' has unsupported direction '{direction}'.", nameof(placementsJson));

            if (entries.Length > 0) entries.Append(',');
            entries.Append("{id=\"").Append(LuaEscape(id)).Append("\",name=\"").Append(LuaEscape(entityName))
                .Append("\",x=").Append(x.ToString("R", CultureInfo.InvariantCulture))
                .Append(",y=").Append(y.ToString("R", CultureInfo.InvariantCulture))
                .Append(",direction=\"").Append(direction.ToLowerInvariant()).Append("\"}");
        }

        var lua = """
            local function esc(s) return s:gsub('\\', '\\\\'):gsub('"', '\\"') end
            local p = game.get_player(storage.factorio_mcp_player_name)
            if not p then error("Configured Factorio player does not exist") end
            local surface = p.surface
            local placements = { __PLACEMENTS__ }
            local results = {}
            local blocked = 0
            for _, placement in ipairs(placements) do
                local proto = prototypes.entity[placement.name]
                local direction = defines.direction[placement.direction]
                local exists = proto ~= nil
                local can_place = false
                local error_message = nil
                if exists and direction then
                    local ok, result = pcall(function()
                        return surface.can_place_entity{
                            name=placement.name,
                            position={placement.x, placement.y},
                            force=p.force,
                            direction=direction
                        }
                    end)
                    if ok then
                        can_place = result == true
                    else
                        error_message = tostring(result)
                    end
                else
                    if not exists then error_message = "unknown_prototype" else error_message = "invalid_direction" end
                end
                if not can_place then blocked = blocked + 1 end
                local part = '{"id":"'..esc(placement.id)..'","entity_name":"'..esc(placement.name)..'","x":'..placement.x..',"y":'..placement.y..',"direction":"'..esc(placement.direction)..'","prototype_exists":'..tostring(exists)..',"can_place":'..tostring(can_place)
                if error_message then part = part..',"error":"'..esc(error_message)..'"' end
                results[#results+1] = part..'}'
            end
            rcon.print('{"success":true,"checked_count":'..#placements..',"blocked_count":'..blocked..',"results":['..table.concat(results, ',')..']}')
            """.Replace("__PLACEMENTS__", entries.ToString());

        return queue.ExecuteAsync(nameof(CheckEntityPlacementBatch), ct => factorio.ExecuteRawLuaAsync(lua, ct), cancellationToken);
    }
}
