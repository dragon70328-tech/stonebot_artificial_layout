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

python -X utf8 -m uvicorn src.http_api:app --host 127.0.0.1 --port $port
