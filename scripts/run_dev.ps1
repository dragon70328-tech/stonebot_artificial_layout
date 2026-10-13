$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$secretFile = Join-Path $scriptDir "dev_env.ps1"

if (Test-Path $secretFile) {
    . $secretFile
}

if ([string]::IsNullOrWhiteSpace($env:OPENAI_API_KEY)) {
    throw "OPENAI_API_KEY is not set. Put it in scripts/dev_env.ps1"
}

if ([string]::IsNullOrWhiteSpace($env:OPENAI_BASE_URL)) {
    $env:OPENAI_BASE_URL = "https://api.deepseek.com"
}

if ([string]::IsNullOrWhiteSpace($env:OPENAI_MODEL)) {
    $env:OPENAI_MODEL = "deepseek-chat"
}

$projectRoot = (Resolve-Path (Join-Path $scriptDir "..")).Path
Set-Location $projectRoot

$port = if ($env:STONEBOT_PORT) { $env:STONEBOT_PORT } else { "8002" }

# 优先用项目自带虚拟环境；没有 .venv 时回退到 PATH 上的 python。
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
$python = if (Test-Path $venvPython) { $venvPython } else { "python" }
Write-Host "使用解释器: $python"

& $python -X utf8 -m uvicorn src.http_api:app --host 127.0.0.1 --port $port
