param([switch]$IncludeXls)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $projectRoot
try {
    if (-not (Test-Path -LiteralPath '.venv/Scripts/python.exe')) {
        python -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw 'Could not create the virtual environment.' }
    }
    $extra = if ($IncludeXls) { '.[xls]' } else { '.' }
    & ./.venv/Scripts/python.exe -m pip install -e $extra
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
    if (-not (Test-Path -LiteralPath '.env')) { Copy-Item -LiteralPath '.env.example' -Destination '.env' }
    Write-Output 'Ready. Fill in your local .env and select .venv in VS Code.'
} finally { Pop-Location }
