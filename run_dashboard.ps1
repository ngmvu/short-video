$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$activateScript = Join-Path $projectRoot ".venv\Scripts\Activate.ps1"

if (-not (Test-Path -LiteralPath $activateScript)) {
    throw "Python virtual environment not found. Create it with: py -3.10 -m venv .venv"
}

Set-Location -LiteralPath $projectRoot
. $activateScript
python web_app.py
