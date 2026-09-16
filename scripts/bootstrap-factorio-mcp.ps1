$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$UpstreamUrl = "https://github.com/sbarisic/FactorioMCP.git"
$UpstreamCommit = "f2fca61707efe3107e7a3738bb83d1505b0126f7"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$Target = Join-Path $RepoRoot "src/FactorioMCP"
$PlayerTargetPatch = Join-Path $RepoRoot "patches/FactorioPlayerTarget.cs"
$BootstrapToolsPatch = Join-Path $RepoRoot "patches/BootstrapStateTools.cs"
$FactoryPlanningToolsPatch = Join-Path $RepoRoot "patches/FactoryPlanningTools.cs"

function Write-Utf8NoBom([string]$Path, [string]$Content) {
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Content, $encoding)
}

function Assert-Command([string]$Name) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Required command '$Name' was not found in PATH."
    }
}

Assert-Command "git"
Assert-Command "dotnet"

if (Test-Path $Target) {
    Write-Host "Removing previous generated FactorioMCP tree: $Target"
    Remove-Item -Recurse -Force $Target
}

Write-Host "Cloning FactorioMCP..."
git clone --quiet $UpstreamUrl $Target
if ($LASTEXITCODE -ne 0) { throw "git clone failed" }

git -C $Target checkout --quiet --detach $UpstreamCommit
if ($LASTEXITCODE -ne 0) { throw "git checkout $UpstreamCommit failed" }

Write-Host "Applying multiplayer player-target patch..."
$needle = "game.connected_players[1]"
$replacement = "game.get_player(storage.factorio_mcp_player_name)"
$replacementCount = 0

Get-ChildItem -Path $Target -Recurse -Filter "*.cs" -File | ForEach-Object {
    $content = [System.IO.File]::ReadAllText($_.FullName)
    if ($content.Contains($needle)) {
        $matches = ([regex]::Matches($content, [regex]::Escape($needle))).Count
        $content = $content.Replace($needle, $replacement)
        Write-Utf8NoBom $_.FullName $content
        $replacementCount += $matches
    }
}

if ($replacementCount -eq 0) {
    throw "Upstream no longer contains '$needle'. Review the patch before changing the pinned commit."
}

Copy-Item -Force $PlayerTargetPatch (Join-Path $Target "FactorioMCP/Services/FactorioPlayerTarget.cs")
Copy-Item -Force $BootstrapToolsPatch (Join-Path $Target "FactorioMCP/Tools/BootstrapStateTools.cs")
Copy-Item -Force $FactoryPlanningToolsPatch (Join-Path $Target "FactorioMCP/Tools/FactoryPlanningTools.cs")

# Factorio 2.0 electric poles no longer expose LuaEntity.neighbours. The pinned
# upstream topology tool still uses pole.neighbours.copper, which raises
# "Neighbours can't be used on this entity". Read real copper-wire connections
# through LuaWireConnector instead (Factorio 2.0.72 API).
$energyServicePath = Join-Path $Target "FactorioMCP/Services/EnergyService.cs"
$energyService = [System.IO.File]::ReadAllText($energyServicePath).Replace("`r`n", "`n")

# Normalize the here-strings as well. On Windows with Git autocrlf they otherwise
# contain CRLF while $energyService above has already been normalized to LF, making
# an exact Contains/Replace fail even though the upstream block is unchanged.
$oldTopologyBlock = (@'
                    local nb = {}
                    for _, n in pairs(pole.neighbours.copper) do
                        if n.electric_network_id == nid then
                            nb[#nb+1] = '{"name":"'..esc(n.name)..'","x":'..string.format("%.1f",n.position.x)..',"y":'..string.format("%.1f",n.position.y)..'}'
                        end
                    end
'@).Replace("`r`n", "`n")
$newTopologyBlock = (@'
                    local nb = {}
                    local connector = pole.get_wire_connector(defines.wire_connector_id.pole_copper, false)
                    if connector then
                        for _, connection in pairs(connector.real_connections) do
                            local target_connector = connection.target
                            local n = target_connector and target_connector.owner or nil
                            if n and n.valid and n.type == "electric-pole" and n.electric_network_id == nid then
                                nb[#nb+1] = '{"name":"'..esc(n.name)..'","x":'..string.format("%.1f",n.position.x)..',"y":'..string.format("%.1f",n.position.y)..'}'
                            end
                        end
                    end
'@).Replace("`r`n", "`n")
if (-not $energyService.Contains($oldTopologyBlock)) {
    throw "Could not patch EnergyService topology: upstream neighbour block changed."
}
$energyService = $energyService.Replace($oldTopologyBlock, $newTopologyBlock)
if ($energyService.Contains("pole.neighbours.copper")) {
    throw "EnergyService topology patch incomplete: pole.neighbours.copper remains."
}
Write-Utf8NoBom $energyServicePath $energyService

# Some Factorio entities (notably certain belt-like entities) do not have a
# unit_number. The pinned flow graph concatenates unit_number unconditionally,
# which crashes with "attempt to concatenate field 'unit_number' (a nil value)".
# Fall back to a stable name+position key when no unit number is available.
$flowServicePath = Join-Path $Target "FactorioMCP/Services/FlowService.cs"
$flowService = [System.IO.File]::ReadAllText($flowServicePath)
$oldFlowKey = '                local key = from_e.unit_number..":"..to_e.unit_number'
$newFlowKey = @'
                local from_key = from_e.unit_number and tostring(from_e.unit_number) or (from_e.name..":"..string.format("%.3f", from_e.position.x)..":"..string.format("%.3f", from_e.position.y))
                local to_key = to_e.unit_number and tostring(to_e.unit_number) or (to_e.name..":"..string.format("%.3f", to_e.position.x)..":"..string.format("%.3f", to_e.position.y))
                local key = from_key..":"..to_key..":"..kind
'@
$newFlowKey = $newFlowKey.TrimEnd("`r", "`n")
if (-not $flowService.Contains($oldFlowKey)) {
    throw "Could not patch FlowService entity key: upstream flow key changed."
}
$flowService = $flowService.Replace($oldFlowKey, $newFlowKey)
if ($flowService.Contains('from_e.unit_number..":"..to_e.unit_number')) {
    throw "FlowService patch incomplete: unsafe unit_number concatenation remains."
}
Write-Utf8NoBom $flowServicePath $flowService

# Harden the pinned entity-prototype query for Factorio 2.0. get_max_health() is
# the replacement for max_health, and restricted prototype accessors such as
# get_crafting_speed() must not be called blindly on inserters, poles, chests or
# mining drills. pcall makes optional/restricted properties genuinely optional.
$worldServicePath = Join-Path $Target "FactorioMCP/Services/FactorioService.World.cs"
$worldService = [System.IO.File]::ReadAllText($worldServicePath).Replace("`r`n", "`n")
$oldPrototypeHealth = "(proto.max_health or 0)"
if (-not $worldService.Contains($oldPrototypeHealth)) {
    throw "Could not patch entity prototype max health: upstream prototype query changed."
}
$worldService = $worldService.Replace($oldPrototypeHealth, "proto.get_max_health()")

$oldCraftingSpeedBlock = (@'
            if proto.get_crafting_speed then
                parts[#parts+1] = '"crafting_speed":'..proto.get_crafting_speed()
            end
'@).Replace("`r`n", "`n")
$newCraftingSpeedBlock = (@'
            local ok_crafting_speed, crafting_speed = pcall(function() return proto.get_crafting_speed() end)
            if ok_crafting_speed and crafting_speed ~= nil then
                parts[#parts+1] = '"crafting_speed":'..crafting_speed
            end
'@).Replace("`r`n", "`n")
if (-not $worldService.Contains($oldCraftingSpeedBlock)) {
    throw "Could not patch entity prototype crafting speed: upstream prototype query changed."
}
$worldService = $worldService.Replace($oldCraftingSpeedBlock, $newCraftingSpeedBlock)

$oldMiningSpeedBlock = (@'
            if proto.mining_speed then
                parts[#parts+1] = '"mining_speed":'..proto.mining_speed
            end
'@).Replace("`r`n", "`n")
$newMiningSpeedBlock = (@'
            local ok_mining_speed, mining_speed = pcall(function() return proto.mining_speed end)
            if ok_mining_speed and mining_speed ~= nil then
                parts[#parts+1] = '"mining_speed":'..mining_speed
            end
'@).Replace("`r`n", "`n")
if (-not $worldService.Contains($oldMiningSpeedBlock)) {
    throw "Could not patch entity prototype mining speed: upstream prototype query changed."
}
$worldService = $worldService.Replace($oldMiningSpeedBlock, $newMiningSpeedBlock)

$oldEnergyUsageBlock = (@'
            if proto.energy_usage then
                parts[#parts+1] = '"energy_usage":'..proto.energy_usage
            end
'@).Replace("`r`n", "`n")
$newEnergyUsageBlock = (@'
            local ok_energy_usage, energy_usage = pcall(function() return proto.energy_usage end)
            if ok_energy_usage and energy_usage ~= nil then
                parts[#parts+1] = '"energy_usage":'..energy_usage
            end
            local ok_burner, burner = pcall(function() return proto.burner_prototype end)
            if ok_burner and burner then
                parts[#parts+1] = '"has_burner":true'
                parts[#parts+1] = '"burner_effectivity":'..(burner.effectivity or 1)
                local categories = {}
                for category, _ in pairs(burner.fuel_categories or {}) do categories[#categories+1] = category end
                table.sort(categories)
                local category_parts = {}
                for _, category in ipairs(categories) do category_parts[#category_parts+1] = '"'..esc(category)..'"' end
                parts[#parts+1] = '"burner_fuel_categories":['..table.concat(category_parts, ',')..']'
            else
                parts[#parts+1] = '"has_burner":false'
            end
'@).Replace("`r`n", "`n")
if (-not $worldService.Contains($oldEnergyUsageBlock)) {
    throw "Could not patch entity prototype energy source data: upstream prototype query changed."
}
$worldService = $worldService.Replace($oldEnergyUsageBlock, $newEnergyUsageBlock)

if ($worldService.Contains("proto.max_health") -or $worldService.Contains("..proto.get_crafting_speed()")) {
    throw "Entity prototype patch incomplete: unsafe Factorio 1.x/restricted prototype access remains."
}
Write-Utf8NoBom $worldServicePath $worldService

$programPath = Join-Path $Target "FactorioMCP/Program.cs"
$program = [System.IO.File]::ReadAllText($programPath)
$programNeedle = "    .AddSingleton<FactorioService>()"
if (-not $program.Contains($programNeedle)) {
    throw "Could not patch Program.cs: FactorioService registration changed upstream."
}
$program = $program.Replace(
    $programNeedle,
    "$programNeedle`r`n    .AddSingleton<FactorioPlayerTarget>()"
)

# MCP stdio requires stdout to contain JSON-RPC only. Route all .NET console logs to stderr.
$loggingUsing = "using Microsoft.Extensions.Logging;"
if (-not $program.Contains($loggingUsing)) {
    $hostUsing = "using Microsoft.Extensions.Hosting;"
    if (-not $program.Contains($hostUsing)) {
        throw "Could not patch Program.cs logging imports: hosting using changed upstream."
    }
    $program = $program.Replace($hostUsing, "$hostUsing`r`n$loggingUsing")
}

$builderNeedle = "var builder = Host.CreateApplicationBuilder(args);"
if (-not $program.Contains($builderNeedle)) {
    throw "Could not patch Program.cs logging: host builder initialization changed upstream."
}
$loggingPatch = @(
    $builderNeedle,
    "",
    "builder.Logging.ClearProviders();",
    "builder.Logging.AddConsole(options =>",
    "{",
    "    options.LogToStandardErrorThreshold = LogLevel.Trace;",
    "});"
) -join "`r`n"
$program = $program.Replace($builderNeedle, $loggingPatch)
Write-Utf8NoBom $programPath $program

$rconServicePath = Join-Path $Target "FactorioMCP/Services/RconConnectionService.cs"
$rconService = [System.IO.File]::ReadAllText($rconServicePath)
$ctorNeedle = "    FactorioService factorioService,`n    IConfiguration configuration,"
if (-not $rconService.Contains($ctorNeedle)) {
    # Upstream file can use CRLF after clone on Windows.
    $ctorNeedle = "    FactorioService factorioService,`r`n    IConfiguration configuration,"
}
if (-not $rconService.Contains($ctorNeedle)) {
    throw "Could not patch RconConnectionService constructor; upstream layout changed."
}
$lineBreak = if ($ctorNeedle.Contains("`r`n")) { "`r`n" } else { "`n" }
$ctorReplacement = "    FactorioService factorioService,$lineBreak    FactorioPlayerTarget playerTarget,$lineBreak    IConfiguration configuration,"
$rconService = $rconService.Replace($ctorNeedle, $ctorReplacement)

$methodNeedle = "    private async Task InitializeGameListenersAsync(CancellationToken cancellationToken)$lineBreak    {$lineBreak        try"
if (-not $rconService.Contains($methodNeedle)) {
    throw "Could not patch RconConnectionService initialization method; upstream layout changed."
}
$methodReplacement = @(
    "    private async Task InitializeGameListenersAsync(CancellationToken cancellationToken)",
    "    {",
    '        var playerName = configuration["FACTORIO_PLAYER_NAME"];',
    "        if (string.IsNullOrWhiteSpace(playerName))",
    "        {",
    '            throw new InvalidOperationException("FACTORIO_PLAYER_NAME is required for multiplayer-safe targeting.");',
    "        }",
    "",
    "        await playerTarget.InitializeAsync(playerName, cancellationToken);",
    '        logger.LogInformation("FactorioMCP target player initialized: {PlayerName}", playerName);',
    "",
    "        try"
) -join $lineBreak
$rconService = $rconService.Replace($methodNeedle, $methodReplacement)
Write-Utf8NoBom $rconServicePath $rconService

$remaining = Get-ChildItem -Path $Target -Recurse -Filter "*.cs" -File |
    Select-String -SimpleMatch $needle
if ($remaining) {
    throw "Patch incomplete: connected_players[1] remains in C# source."
}

Write-Host "Patched $replacementCount player references."
Write-Host "Installed structured factory-layout planning tool."
Write-Host "Patched Factorio 2.0 power-topology wire traversal."
Write-Host "Patched flow-graph fallback entity keys."
Write-Host "Patched Factorio 2.0 entity prototype compatibility/query safety."
Write-Host "Building FactorioMCP..."
dotnet build (Join-Path $Target "FactorioMCP.sln")
if ($LASTEXITCODE -ne 0) { throw "dotnet build failed" }

Write-Host ""
Write-Host "FactorioMCP is ready at: $Target"
Write-Host "Pinned upstream commit: $UpstreamCommit"
