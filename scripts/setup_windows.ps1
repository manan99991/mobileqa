# One-time setup on Windows (Android only — iOS needs a Mac or a device cloud, see README).
# Run in an elevated PowerShell:  Set-ExecutionPolicy Bypass -Scope Process; .\scripts\setup_windows.ps1
$ErrorActionPreference = "Continue"

Write-Host "== winget packages"
winget install -e --id OpenJS.NodeJS.LTS
winget install -e --id Python.Python.3.12
winget install -e --id Microsoft.OpenJDK.17
winget install -e --id Google.AndroidStudio      # brings SDK, emulator, adb

Write-Host "== Appium"
npm install -g appium
appium driver install uiautomator2

Write-Host "== mobileqa"
python -m pip install --upgrade pip
python -m pip install -e (Join-Path $PSScriptRoot "..")

Write-Host @"

Done. Next (new terminal):
  1. Android Studio > More Actions > Virtual Device Manager > Create device > Pixel 6, API 34 > Finish, then press Play.
  2. setx ANTHROPIC_API_KEY sk-ant-...       (reopen the terminal afterwards)
  3. appium --relaxed-security               (leave it running)
  4. mobileqa doctor apps\reckato\app.android.yaml
  5. set RECKATO_APK=C:\path\to\qa-build.apk && mobileqa explore apps\reckato\app.android.yaml
"@
