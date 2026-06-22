$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvPython = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $venvPython)) {
    Write-Host "Error: .venv not found. This tool requires the bundled .venv folder." -ForegroundColor Red
    Read-Host "Press Enter to exit"
    exit 1
}

Write-Host ""
Write-Host "  3D Hole Annotator Tool" -ForegroundColor Green
Write-Host "  Browser will open at http://127.0.0.1:8080" -ForegroundColor Cyan
Write-Host ""
& $venvPython (Join-Path $root "app.py")
