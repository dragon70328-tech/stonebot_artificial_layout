$ErrorActionPreference = "Stop"

# Windows PowerShell 5.1 otherwise decodes MCP stdin using the active legacy
# code page, corrupting Chinese DXF and artifact paths before Python sees them.
$utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::InputEncoding = $utf8
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8

$pluginRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$repoRoot = (Resolve-Path (Join-Path $pluginRoot "..\..")).Path

$python = $env:STONEBOT_PYTHON
if (-not $python) {
    $venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $venvPython) {
        $python = $venvPython
    }
}
if (-not $python) {
    $pythonCommand = Get-Command python -ErrorAction Stop
    $python = $pythonCommand.Source
}

Set-Location -LiteralPath $repoRoot
$arguments = @("-X", "utf8", "-m", "src.mcp_server")
if ($env:STONEBOT_MCP_DATA_ROOT) {
    $arguments += @("--data-root", $env:STONEBOT_MCP_DATA_ROOT)
}

& $python @arguments
exit $LASTEXITCODE
