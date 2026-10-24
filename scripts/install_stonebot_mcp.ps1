$ErrorActionPreference = "Stop"

$repo = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$server = Join-Path $repo "plugins\stonebot\run_mcp.ps1"
if (-not (Test-Path -LiteralPath $server)) {
    throw "Stonebot MCP launcher not found: $server"
}
if (-not (Get-Command codex -ErrorAction SilentlyContinue)) {
    throw "Codex CLI was not found on PATH."
}

codex mcp add stonebot -- powershell.exe -NoProfile -ExecutionPolicy Bypass -File $server
if ($LASTEXITCODE -ne 0) {
    throw "Failed to register the Stonebot MCP server."
}

Write-Host "Registered Stonebot MCP server."
Write-Host "Restart Codex or use /mcp in the TUI to verify the connection."
