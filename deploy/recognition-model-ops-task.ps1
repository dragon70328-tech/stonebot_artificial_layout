param(
    [Parameter(Mandatory=$true)][string]$ProjectRoot,
    [string]$PythonExecutable="python",
    [string]$FeedbackDir,
    [string]$OutputDir,
    [switch]$ApplyRetention
)

$ErrorActionPreference="Stop"
Set-Location -LiteralPath $ProjectRoot
if (-not $FeedbackDir) { $FeedbackDir=Join-Path $ProjectRoot "recognition_models\global_feedback" }
if (-not $OutputDir) { $OutputDir=Join-Path $ProjectRoot "recognition_models" }
$logDir=Join-Path $OutputDir "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logPath=Join-Path $logDir ("operations_"+(Get-Date -Format "yyyyMMdd_HHmmss")+".log")
$arguments=@(
    "-X","utf8",
    (Join-Path $ProjectRoot "scripts\run_recognition_model_ops.py"),
    "--feedback-dir",$FeedbackDir,
    "--output-dir",$OutputDir,
    "--min-new-feedback","20",
    "--max-interval-days","30",
    "--retention-max-age-days","180",
    "--retention-max-files-per-tenant","500"
)
if ($ApplyRetention) { $arguments+="--apply-retention" }
& $PythonExecutable @arguments *>> $logPath
exit $LASTEXITCODE
