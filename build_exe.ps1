# Builds dist\SignalBloom-Agent.exe, a single-file Windows program that runs
# without Python. Run from the repo root:  .\build_exe.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$python = ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Error "No .venv found. Create it first: py -3.12 -m venv .venv (see README)."
}

Write-Host "Installing build requirements..."
& $python -m pip install --quiet -r requirements-build.txt
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue

# Only imported modules and config.example.yaml (the template) go in.
# Your config.yaml, data\ and tokens are never added.
& $python -m PyInstaller --onefile --console --clean --noconfirm `
    --name SignalBloom-Agent `
    --add-data "config.example.yaml;." `
    run_agent.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

$exe = "dist\SignalBloom-Agent.exe"

# Safety check: stop if any local state somehow ended up inside the exe.
$contents = & $python -m PyInstaller.utils.cliutils.archive_viewer --list --recursive $exe
if ($LASTEXITCODE -ne 0) { throw "Could not list the exe's contents" }
$forbidden = "(^|[\\/\s'])(config\.yaml|data[\\/]|buffer\.jsonl|commands\.json|rejected\.jsonl|agent\.log|demo_fault|\.fake_cloud_state\.json)"
$bad = $contents | Select-String -Pattern $forbidden
if ($bad) {
    Remove-Item -Force $exe
    $bad | ForEach-Object { Write-Host "  $_" }
    throw "Local state found inside the exe. It was deleted; do not share it."
}

Write-Host ""
Write-Host "Built $exe ($([math]::Round((Get-Item $exe).Length / 1MB, 1)) MB)"
