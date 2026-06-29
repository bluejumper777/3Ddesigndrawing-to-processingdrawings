$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvPath = Join-Path $root ".venv"
$venvPython = Join-Path $venvPath "Scripts\python.exe"

# Output to a SHORT path to avoid 260-char limit
$distPath = "C:\HoleAnnotator_v5"

Write-Host ""
Write-Host "  3D Hole Annotator v5.0 - Build Script" -ForegroundColor Cyan
Write-Host "  Output: $distPath" -ForegroundColor Gray
Write-Host ""

if (-not (Test-Path $venvPython)) {
    Write-Host "ERROR: .venv not found." -ForegroundColor Red
    Read-Host "Press Enter"
    exit 1
}

Write-Host "  [OK] Source venv found" -ForegroundColor Green

if (Test-Path $distPath) {
    Write-Host "  Cleaning old build..." -ForegroundColor Gray
    Remove-Item -LiteralPath $distPath -Recurse -Force
}
New-Item -ItemType Directory -Path $distPath -Force | Out-Null

# Copy Python runtime
Write-Host "  Copying Python runtime..." -ForegroundColor Gray
$pythonDist = Join-Path $distPath "python"
New-Item -ItemType Directory -Path $pythonDist -Force | Out-Null

$basePython = & $venvPython -c "import sys; print(sys.base_prefix)"
$pyVer = & $venvPython -c "import sys; print(f'{sys.version_info.major}{sys.version_info.minor}')"

# Copy REAL python.exe from base installation (not venv shim!)
Copy-Item (Join-Path $basePython "python.exe") -Destination $pythonDist
Copy-Item (Join-Path $basePython "pythonw.exe") -Destination $pythonDist -ErrorAction SilentlyContinue

# DLLs
$dlls = @("python$pyVer.dll", "python3.dll", "vcruntime140.dll", "vcruntime140_1.dll")
foreach ($dll in $dlls) {
    $p = Join-Path $basePython $dll
    if (Test-Path $p) { Copy-Item $p -Destination $pythonDist }
}

# DLLs folder
$dllsDir = Join-Path $basePython "DLLs"
if (Test-Path $dllsDir) {
    robocopy $dllsDir (Join-Path $pythonDist "DLLs") /E /NFL /NDL /NJH /NJS /NC /NS /NP | Out-Null
}

# Standard library - use robocopy for long path support
$libDir = Join-Path $basePython "Lib"
$libDist = Join-Path $pythonDist "Lib"
Write-Host "  Copying standard library..." -ForegroundColor Gray
robocopy $libDir $libDist /E /NFL /NDL /NJH /NJS /NC /NS /NP /XD test tests idle_test tkinter turtledemo idlelib ensurepip __pycache__ | Out-Null
Write-Host "  [OK] Standard library" -ForegroundColor Green

# Site-packages - use robocopy
$sitePackagesSrc = Join-Path $venvPath "Lib\site-packages"
$sitePackagesDst = Join-Path $pythonDist "Lib\site-packages"
Write-Host "  Copying site-packages (CadQuery/OCP/FastAPI)..." -ForegroundColor Gray
robocopy $sitePackagesSrc $sitePackagesDst /E /NFL /NDL /NJH /NJS /NC /NS /NP /XD __pycache__ | Out-Null
Write-Host "  [OK] Site-packages" -ForegroundColor Green

# _pth file
$pthContent = "Lib`nLib\site-packages`nDLLs`n.`n..`nimport site"
Set-Content -Path (Join-Path $pythonDist "python${pyVer}._pth") -Value $pthContent -Encoding ASCII

# Application files
Write-Host "  Copying app files..." -ForegroundColor Gray
Copy-Item (Join-Path $root "app.py") -Destination $distPath
Copy-Item (Join-Path $root "annotator.py") -Destination $distPath
robocopy (Join-Path $root "static") (Join-Path $distPath "static") /E /NFL /NDL /NJH /NJS /NC /NS /NP | Out-Null
New-Item -ItemType Directory -Path (Join-Path $distPath "output") -Force | Out-Null
Write-Host "  [OK] App files" -ForegroundColor Green

# Launcher
Write-Host "  Creating launcher..." -ForegroundColor Gray
$cmdLines = @()
$cmdLines += '@echo off'
$cmdLines += 'title Hole Annotator v5.0'
$cmdLines += 'cd /d "%~dp0"'
$cmdLines += 'echo.'
$cmdLines += 'echo   3D Hole Annotator Tool v5.0'
$cmdLines += 'echo   Starting server...'
$cmdLines += 'echo.'
$cmdLines += 'if not exist "python\python.exe" goto :nopython'
$cmdLines += '"python\python.exe" app.py'
$cmdLines += 'if errorlevel 1 goto :crashed'
$cmdLines += 'goto :eof'
$cmdLines += ':nopython'
$cmdLines += 'echo [ERROR] python\python.exe not found'
$cmdLines += 'pause'
$cmdLines += 'goto :eof'
$cmdLines += ':crashed'
$cmdLines += 'echo.'
$cmdLines += 'echo [ERROR] App exited with error.'
$cmdLines += 'echo Install VC++ Runtime: https://aka.ms/vs/17/release/vc_redist.x64.exe'
$cmdLines += 'pause'
$cmdContent = $cmdLines -join "`r`n"
[System.IO.File]::WriteAllText((Join-Path $distPath "Start_HoleAnnotator.cmd"), $cmdContent, [System.Text.Encoding]::Default)
Write-Host "  [OK] Launcher" -ForegroundColor Green

# Size report
$totalSize = (Get-ChildItem -Path $distPath -Recurse -File | Measure-Object -Property Length -Sum).Sum
$sizeMB = [math]::Round($totalSize / 1MB, 1)

Write-Host ""
Write-Host "  BUILD COMPLETE!" -ForegroundColor Green
Write-Host "  Output: $distPath" -ForegroundColor Cyan
Write-Host "  Size:   $sizeMB MB" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Zip and share the folder to distribute." -ForegroundColor Yellow
Write-Host ""
