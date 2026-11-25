# make_package.ps1
# Builds the self-contained RapidRadar.exe with PyInstaller.
#
# Usage (from the project folder):
#   PowerShell -ExecutionPolicy Bypass -File make_package.ps1             # RapidRadar.exe
#   PowerShell -ExecutionPolicy Bypass -File make_package.ps1 -Offline    # + offline package (zip)
#   PowerShell -ExecutionPolicy Bypass -File make_package.ps1 -Test       # run the tests first
#   PowerShell -ExecutionPolicy Bypass -File make_package.ps1 -KeepBuild  # keep build\ and dist\
#
# The build always uses the project's .venv with the exact versions pinned in
# requirements-build.txt (created / synced automatically), so every build is
# reproducible and independent of whatever is installed globally.
#
# Output:
#   RapidRadar.exe                 single EXE (downloads Chromium on first launch)
#   RapidRadar_Offline.zip         (-Offline) EXE + Chromium, for servers without internet

param(
    [switch]$Offline,     # also produce RapidRadar_Offline.zip with the browser bundled
    [switch]$Test,        # run the test suite before building
    [switch]$KeepBuild    # keep PyInstaller's build\ and dist\ folders afterwards
)

$ErrorActionPreference = "Stop"
$Root      = $PSScriptRoot
$SpecFile  = Join-Path $Root "RapidRadar.spec"
$DistDir   = Join-Path $Root "dist"
$BuildDir  = Join-Path $Root "build"
$OutExe    = Join-Path $DistDir "RapidRadar.exe"
$FinalExe  = Join-Path $Root "RapidRadar.exe"
$Venv      = Join-Path $Root ".venv"
$Py        = Join-Path $Venv "Scripts\python.exe"
$BuildReqs = Join-Path $Root "requirements-build.txt"
$DevReqs   = Join-Path $Root "requirements-dev.txt"

function Step($text) { Write-Host ""; Write-Host $text -ForegroundColor Yellow }
function Fail($text) { Write-Host ""; Write-Host "BUILD FAILED: $text" -ForegroundColor Red; exit 1 }

Set-Location $Root
Write-Host ""
Write-Host "================================================================="
Write-Host "  RapidRadar -- EXE Builder"
Write-Host "================================================================="

# -- 1. Build environment (.venv with pinned versions) ------------------------
Step "[1/5] Preparing build environment (.venv, pinned versions) ..."
if (-not (Test-Path $Py)) {
    Write-Host "      Creating .venv ..."
    python -m venv $Venv
    if ($LASTEXITCODE -ne 0) { Fail "could not create .venv (is Python 3.11 x64 on PATH?)" }
}
$reqs = if ($Test) { $DevReqs } else { $BuildReqs }
& $Py -m pip install --disable-pip-version-check --quiet -r $reqs
if ($LASTEXITCODE -ne 0) { Fail "pip install -r $(Split-Path $reqs -Leaf) failed" }
$pyVer = & $Py --version
$piVer = & $Py -m PyInstaller --version
Write-Host "      $pyVer, PyInstaller $piVer" -ForegroundColor Green

# -- 2. Tests (optional) -------------------------------------------------------
if ($Test) {
    Step "[2/5] Running tests ..."
    & $Py -m pytest -q
    if ($LASTEXITCODE -ne 0) { Fail "tests failed - fix them before building" }
} else {
    Step "[2/5] Skipping tests (pass -Test to run them)."
}

# -- 3. Favicon + PyInstaller --------------------------------------------------
Step "[3/5] Building RapidRadar.exe (a few minutes) ..."
& $Py generate_favicon.py
if ($LASTEXITCODE -ne 0) { Write-Warning "Favicon generation failed - EXE will have no custom icon." }
if (Test-Path $BuildDir) { Remove-Item $BuildDir -Recurse -Force }
if (Test-Path $DistDir)  { Remove-Item $DistDir  -Recurse -Force }
& $Py -m PyInstaller $SpecFile --noconfirm --distpath $DistDir --workpath $BuildDir
if ($LASTEXITCODE -ne 0) { Fail "PyInstaller exited with code $LASTEXITCODE" }
if (-not (Test-Path $OutExe)) { Fail "expected output not found: $OutExe" }
Copy-Item $OutExe $FinalExe -Force
$sizeMB = [math]::Round((Get-Item $FinalExe).Length / 1MB, 1)
Write-Host "      RapidRadar.exe ($sizeMB MB)" -ForegroundColor Green

# -- 4. Offline package (optional) ---------------------------------------------
$OfflineZip = Join-Path $Root "RapidRadar_Offline.zip"
if ($Offline) {
    Step "[4/5] Building offline package (downloads Chromium once, ~300 MB) ..."
    $Stage    = Join-Path $Root "build_offline\RapidRadar"
    $Browsers = Join-Path $Stage "ms-playwright"
    if (Test-Path (Join-Path $Root "build_offline")) { Remove-Item (Join-Path $Root "build_offline") -Recurse -Force }
    New-Item -ItemType Directory -Force $Browsers | Out-Null
    Copy-Item $FinalExe $Stage
    # Same Playwright version as inside the EXE, so the browser build matches its driver.
    $env:PLAYWRIGHT_BROWSERS_PATH = $Browsers
    & $Py -m playwright install chromium
    $rc = $LASTEXITCODE
    Remove-Item Env:\PLAYWRIGHT_BROWSERS_PATH
    if ($rc -ne 0) { Fail "playwright install chromium failed (internet / proxy?)" }
    Set-Content (Join-Path $Stage ".rr_browsers_installed") "installed (offline package)"
    @"
RapidRadar - offline package
============================
1. Extract this folder anywhere on the target machine (keep ms-playwright next to the EXE).
2. Double-click RapidRadar.exe - no download is needed; the web UI opens on http://127.0.0.1:5000.
3. For SQL Server scans install "Microsoft ODBC Driver 18 for SQL Server" on that machine.
"@ | Set-Content (Join-Path $Stage "README.txt") -Encoding UTF8
    if (Test-Path $OfflineZip) { Remove-Item $OfflineZip -Force }
    Compress-Archive -Path $Stage -DestinationPath $OfflineZip -CompressionLevel Optimal
    Remove-Item (Join-Path $Root "build_offline") -Recurse -Force
    $zipMB = [math]::Round((Get-Item $OfflineZip).Length / 1MB, 1)
    Write-Host "      RapidRadar_Offline.zip ($zipMB MB)" -ForegroundColor Green
} else {
    Step "[4/5] Skipping offline package (pass -Offline to build it)."
}

# -- 5. Clean up ---------------------------------------------------------------
if ($KeepBuild) {
    Step "[5/5] Keeping build\ and dist\ (-KeepBuild)."
} else {
    Step "[5/5] Removing build\ and dist\ ..."
    if (Test-Path $BuildDir) { Remove-Item $BuildDir -Recurse -Force }
    if (Test-Path $DistDir)  { Remove-Item $DistDir  -Recurse -Force }
}

Write-Host ""
Write-Host "================================================================="
Write-Host "  BUILD SUCCESSFUL" -ForegroundColor Green
Write-Host "  $FinalExe  ($sizeMB MB)"
if ($Offline) { Write-Host "  $OfflineZip" }
Write-Host ""
Write-Host "  Online use : copy RapidRadar.exe - Chromium downloads on first launch."
if ($Offline) { Write-Host "  Offline use: copy and extract RapidRadar_Offline.zip - nothing to download." }
Write-Host "================================================================="
