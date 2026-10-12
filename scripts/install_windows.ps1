<#
.SYNOPSIS
  satrx telepítése Windowson (fejlesztői / tesztgép): virtuális környezet, függőségek, webes felület, konfiguráció.

.DESCRIPTION
  A projekt mappájában létrehozza a .venv környezetet, telepíti a csomagot, megépíti a webes felületet (ha van Node.js),
  létrehozza a config\local.yaml fájlt véletlen API-tokennel, és futtatja a `satrx doctor` ellenőrzést.
  Többször futtatható; a meglévő konfigurációt nem írja felül.

.PARAMETER Backend
  Az SDR-backend a konfigurációban: mock (alap, hardver nélkül), rtlsdr vagy soapy.

.PARAMETER DryRun
  Csak kiírja, mit tenne.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\install_windows.ps1 -Backend rtlsdr
#>
param(
    [string]$Python = "python",
    [ValidateSet("mock", "rtlsdr", "soapy")][string]$Backend = "mock",
    [switch]$BuildWeb,
    [switch]$SkipDoctor,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot

function Step([string]$Text) { Write-Host "==> $Text" -ForegroundColor Cyan }
function Run([string]$Display, [scriptblock]$Action) {
    if ($DryRun) { Write-Host "+ $Display" } else { & $Action; if ($LASTEXITCODE -ne 0 -and $null -ne $LASTEXITCODE) { throw "A parancs sikertelen: $Display" } }
}

Set-Location $Root

Step "Python ellenőrzése"
if (-not $DryRun) {
    & $Python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
    if ($LASTEXITCODE -ne 0) { throw "Python 3.11 vagy újabb kell (https://www.python.org/downloads/). Megadható: -Python <út>" }
}

Step "Virtuális környezet és függőségek (.venv)"
$Venv = Join-Path $Root ".venv"
$VenvPython = Join-Path $Venv "Scripts\python.exe"
Run "$Python -m venv .venv" { & $Python -m venv $Venv }
Run ".venv\Scripts\python -m pip install --upgrade pip" { & $VenvPython -m pip install --upgrade pip }
$Extra = if ($Backend -eq "rtlsdr") { "[rtlsdr]" } else { "" }
Run ".venv\Scripts\pip install -e `".$Extra`"" { & $VenvPython -m pip install -e ".$Extra" }

Step "Webes felület"
$Dist = Join-Path $Root "web\dist\index.html"
if (Test-Path $Dist) {
    Write-Host "A web\dist már megvan."
} elseif ((Get-Command npm -ErrorAction SilentlyContinue) -or $DryRun) {
    Run "npm install && npm run build (web mappa)" {
        Push-Location (Join-Path $Root "web"); try { npm install --no-audit --no-fund; npm run build } finally { Pop-Location }
    }
} else {
    Write-Warning "Nincs Node.js, a web\dist hiányzik. Telepítsd a Node.js-t (https://nodejs.org), vagy használj kiadási csomagot."
}

Step "Konfiguráció (config\local.yaml)"
$Config = Join-Path $Root "config\local.yaml"
if (Test-Path $Config) {
    Write-Host "A meglévő konfiguráció megmarad."
    $Token = "(a meglévő konfigurációban)"
} else {
    $Token = if ($DryRun) { "<generált-token>" } else { & $VenvPython -c "import secrets; print(secrets.token_urlsafe(24))" }
    $Yaml = @"
# A telepítő hozta létre; szabadon szerkeszthető (példa: config\local.yaml.example)
server:
  host: 127.0.0.1
  port: 8000
  auth_token: "$Token"
sdr:
  backend: $Backend
"@
    if ($DryRun) { Write-Host "+ write config\local.yaml (backend: $Backend)" } else { Set-Content -Path $Config -Value $Yaml -Encoding UTF8 }
}

Step "Indító parancsfájl (run_satrx.cmd)"
$Launcher = Join-Path $Root "run_satrx.cmd"
if ($DryRun) { Write-Host "+ write run_satrx.cmd" } else {
    Set-Content -Path $Launcher -Encoding ASCII -Value "@echo off`r`ncd /d `"%~dp0`"`r`n`".venv\Scripts\python.exe`" -m satrx %*"
}

if (-not $SkipDoctor) {
    Step "Ellenőrzés (satrx doctor)"
    Run ".venv\Scripts\python -m satrx doctor" { & $VenvPython -m satrx doctor }
}

Write-Host ""
Write-Host "Kész. Indítás:  run_satrx.cmd   (majd: http://localhost:8000)" -ForegroundColor Green
Write-Host "API-token:      $Token"
if ($Backend -eq "rtlsdr") {
    Write-Host "RTL-SDR Windowson: a WinUSB illesztőt a Zadig programmal kell telepíteni (https://zadig.akeo.ie), és az rtlsdr.dll-nek elérhetőnek kell lennie (PATH vagy a python.exe mellett)."
}
