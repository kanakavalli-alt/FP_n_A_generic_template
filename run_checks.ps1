# Full local check: lint the semantic layer, run the test suite, reconcile against the demo sources.
# Copies the project to a short path first (Windows MAX_PATH) and uses the .fpa_venv Python.
$ErrorActionPreference = "Stop"
$src = $PSScriptRoot
$dst = Join-Path $env:USERPROFILE "fpa_run"
$py = Join-Path $env:USERPROFILE ".fpa_venv\Scripts\python.exe"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONDONTWRITEBYTECODE = "1"

Write-Host "== Copying project to $dst" -ForegroundColor Cyan
robocopy $src $dst /MIR /XD __pycache__ out logs data /NFL /NDL /NJH /NJS /NP | Out-Null
Set-Location $dst

$results = [ordered]@{}
function Step($name, [scriptblock]$cmd) {
    Write-Host "`n== $name" -ForegroundColor Cyan
    & $cmd
    $results[$name] = if ($LASTEXITCODE -eq 0) { "PASS" } else { "FAIL ($LASTEXITCODE)" }
}

Step "Build demo source databases" { & $py demo/build_demo_db.py }
Step "Validate semantic layer (live schema check)" { & $py -m fpa_agent validate --live }
Step "Validate client template" { & $py -m fpa_agent --catalog-dir semantic_layer/client_template validate }
Step "Unit + integration tests (pytest)" { & $py -m pytest }
Step "Source reconciliation (catalog + auto tests)" { & $py -m fpa_agent reconcile --auto-months 3 --as-of 2026-10-15 --out out/reconciliation }
Step "ADK app loads" { & $py -c "import sys; sys.path.insert(0,'apps'); import fpa_analyst.agent as a; print(a.root_agent.name, [t.name for t in a.root_agent.tools])" }

Write-Host "`n================ SUMMARY ================" -ForegroundColor Yellow
$results.GetEnumerator() | ForEach-Object { Write-Host ("{0,-48} {1}" -f $_.Key, $_.Value) }
Write-Host "Reconciliation report: $dst\out\reconciliation\reconciliation.md"
