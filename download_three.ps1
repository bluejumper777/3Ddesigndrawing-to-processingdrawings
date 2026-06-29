$dir = Join-Path $PSScriptRoot "static\three"
New-Item -ItemType Directory -Path $dir -Force | Out-Null

Write-Host "Downloading three.module.js..."
Invoke-WebRequest -Uri "https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.module.js" -OutFile (Join-Path $dir "three.module.js")

Write-Host "Downloading OrbitControls.js..."
Invoke-WebRequest -Uri "https://cdn.jsdelivr.net/npm/three@0.160.0/examples/jsm/controls/OrbitControls.js" -OutFile (Join-Path $dir "OrbitControls.js")

Write-Host "Downloading STLLoader.js..."
Invoke-WebRequest -Uri "https://cdn.jsdelivr.net/npm/three@0.160.0/examples/jsm/loaders/STLLoader.js" -OutFile (Join-Path $dir "STLLoader.js")

Write-Host "All downloads complete!"
Get-ChildItem $dir | Select-Object Name, Length
