param(
    [string]$Version = "v1.0.0"
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$DistDir = Join-Path $Root "dist\sk5-change"
$ReleaseDir = Join-Path $Root "release"
$ZipPath = Join-Path $ReleaseDir ("sk5-change-{0}-windows-x64.zip" -f $Version)
$AutoText = ([string][char]0x81EA) + ([string][char]0x52A8)
$LauncherName = ([string][char]0x4E00) + ([string][char]0x952E) + ([string][char]0x542F) + ([string][char]0x52A8) + ".vbs"
$GuideName = ([string][char]0x4F7F) + ([string][char]0x7528) + ([string][char]0x8BF4) + ([string][char]0x660E) + ".txt"

Set-Location $Root

python -m playwright install chromium
python -m PyInstaller --clean --noconfirm sk5-change.spec

if (-not (Test-Path $DistDir)) {
    throw "Build output was not created: $DistDir"
}

New-Item -ItemType Directory -Force -Path $ReleaseDir | Out-Null
Remove-Item $ZipPath -Force -ErrorAction SilentlyContinue

Copy-Item README.md (Join-Path $DistDir "README.md") -Force
Copy-Item requirements.txt (Join-Path $DistDir "requirements.txt") -Force

$BrowserCache = Join-Path $env:LOCALAPPDATA "ms-playwright"
$BundledBrowsers = Join-Path $DistDir "ms-playwright"
if (Test-Path $BrowserCache) {
    Remove-Item $BundledBrowsers -Recurse -Force -ErrorAction SilentlyContinue
    Copy-Item $BrowserCache $BundledBrowsers -Recurse -Force
} else {
    throw "Playwright browser cache was not found: $BrowserCache"
}

$ConfigContent = @"
{
  "test_url": "https://www.usnbweb.red",
  "max_latency_ms": 1000,
  "max_exchange_count": 20,
  "settle_seconds": 10,
  "bind_local_network": true,
  "local_bind_ip": "$AutoText"
}
"@
[System.IO.File]::WriteAllText(
    (Join-Path $DistDir "ip_exchange_config.json"),
    $ConfigContent,
    [System.Text.UTF8Encoding]::new($false)
)

$LauncherContent = @"
Option Explicit
Dim shell, fso, folder, exePath
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
folder = fso.GetParentFolderName(WScript.ScriptFullName)
exePath = fso.BuildPath(folder, "sk5-change.exe")
If Not fso.FileExists(exePath) Then
    MsgBox "Cannot find sk5-change.exe", vbCritical, "Startup failed"
    WScript.Quit 1
End If
shell.CurrentDirectory = folder
shell.Run Chr(34) & exePath & Chr(34), 1, False
"@
[System.IO.File]::WriteAllText(
    (Join-Path $DistDir $LauncherName),
    $LauncherContent,
    [System.Text.Encoding]::ASCII
)

$ReadmeContent = @"
SK5 Change Windows x64 portable package

Usage:
1. Extract this zip to any writable folder.
2. Double-click sk5-change.exe or the bundled VBS launcher.
3. First use: click the login/update-login button and log in.

No Python installation is required.
The package includes the browser runtime used by the app.
The app stores config, logs, and browser login data in this folder.
"@
[System.IO.File]::WriteAllText(
    (Join-Path $DistDir $GuideName),
    $ReadmeContent,
    [System.Text.UTF8Encoding]::new($false)
)

Compress-Archive -Path (Join-Path $DistDir "*") -DestinationPath $ZipPath -Force

Write-Host "Release package created: $ZipPath"
