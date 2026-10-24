$ErrorActionPreference = "Stop"

$repo = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..")).Path
$venvPython = Join-Path $repo ".venv\Scripts\python.exe"
$python = if (Test-Path -LiteralPath $venvPython) {
    $venvPython
} else {
    "python"
}
$server = Join-Path $PSScriptRoot "mcp_server.py"

Set-Location -LiteralPath $repo
& $python -u -X utf8 $server @args
