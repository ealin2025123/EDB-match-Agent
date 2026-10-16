param([Parameter(Mandatory=$true)][string]$Job)
$ErrorActionPreference = 'Stop'
$jobPath = (Resolve-Path -LiteralPath $Job).Path
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $projectRoot
try {
    & ./.venv/Scripts/python.exe -X utf8 -m excel_match_agent run --job $jobPath
    exit $LASTEXITCODE
} finally { Pop-Location }
