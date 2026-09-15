$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$UpstreamUrl = "https://github.com/sbarisic/FactorioMCP.git"
$UpstreamCommit = "f2fca61707efe3107e7a3738bb83d1505b0126f7"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$Target = Join-Path $RepoRoot "src/FactorioMCP"
$PatchFile = Join-Path $RepoRoot "patches/FactorioPlayerTarget.cs"

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

Copy-Item -Force $PatchFile (Join-Path $Target "FactorioMCP/Services/FactorioPlayerTarget.cs")

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
    "        var playerName = configuration[\"FACTORIO_PLAYER_NAME\"];",
    "        if (string.IsNullOrWhiteSpace(playerName))",
    "        {",
    "            throw new InvalidOperationException(\"FACTORIO_PLAYER_NAME is required for multiplayer-safe targeting.\");",
    "        }",
    "",
    "        await playerTarget.InitializeAsync(playerName, cancellationToken);",
    "        logger.LogInformation(\"FactorioMCP target player initialized: {PlayerName}\", playerName);",
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
Write-Host "Building FactorioMCP..."
dotnet build (Join-Path $Target "FactorioMCP.sln")
if ($LASTEXITCODE -ne 0) { throw "dotnet build failed" }

Write-Host ""
Write-Host "FactorioMCP is ready at: $Target"
Write-Host "Pinned upstream commit: $UpstreamCommit"
