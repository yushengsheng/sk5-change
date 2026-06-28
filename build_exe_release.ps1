param(
    [string]$Version = "v1.1.0"
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$ReleaseDir = Join-Path $Root "release"
$LiteSource = Join-Path $Root "dist\sk5-change-lite.exe"
$FullSource = Join-Path $Root "dist\sk5-change-full.exe"
$LiteTarget = Join-Path $ReleaseDir ("sk5-change-{0}-lite-windows-x64.exe" -f $Version)
$FullTarget = Join-Path $ReleaseDir ("sk5-change-{0}-full-windows-x64.exe" -f $Version)

Set-Location $Root

New-Item -ItemType Directory -Force -Path $ReleaseDir | Out-Null
Remove-Item $LiteTarget -Force -ErrorAction SilentlyContinue
Remove-Item $FullTarget -Force -ErrorAction SilentlyContinue

python -m PyInstaller --clean --noconfirm sk5-change-single-lite.spec

if (-not (Test-Path $LiteSource)) {
    throw "Lite executable was not created: $LiteSource"
}

Copy-Item $LiteSource $LiteTarget -Force

python -m playwright install chromium
python -m PyInstaller --clean --noconfirm sk5-change-single-full.spec

if (-not (Test-Path $FullSource)) {
    throw "Full executable was not created: $FullSource"
}

Copy-Item $FullSource $FullTarget -Force

$LiteSizeMb = [math]::Round((Get-Item $LiteTarget).Length / 1MB, 2)
$FullSizeMb = [math]::Round((Get-Item $FullTarget).Length / 1MB, 2)

Write-Host "Lite EXE: $LiteTarget ($LiteSizeMb MB)"
Write-Host "Full EXE: $FullTarget ($FullSizeMb MB)"
