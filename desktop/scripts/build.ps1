# Build TaffyDesktopStudio.exe (pyinstaller onefile + windowed + ico)
# Usage: powershell -ExecutionPolicy Bypass -File scripts/build.ps1
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# Precondition: vendored libs must exist (zero-external-resources rule)
foreach ($lib in @("frontend\vendor\vue.global.prod.js", "frontend\vendor\echarts.min.js")) {
    if (-not (Test-Path (Join-Path $root $lib))) {
        Write-Error "missing $lib - run scripts/vendor.ps1 first"
        exit 1
    }
}

# Entry script for PyInstaller (module entry does not work with -m)
$entry = Join-Path $root "taffy_desktop\__main__.py"
if (-not (Test-Path $entry)) {
@'
from taffy_desktop.app import main

if __name__ == "__main__":
    raise SystemExit(main())
'@ | Set-Content -Path $entry -Encoding UTF8
}

# pyinstaller is a build-time tool only; never added to runtime dependencies
uv run --with pyinstaller pyinstaller `
    --noconfirm --clean `
    --onefile --windowed `
    --name TaffyDesktopStudio `
    --add-data "$root\frontend;frontend" `
    --hidden-import webview.platforms.winforms `
    --hidden-import windows_toasts `
    --collect-submodules keyring.backends `
    $entry

if ($LASTEXITCODE -ne 0) {
    Write-Error "build failed"
    exit 1
}

Write-Host ""
Write-Host "output: $root\dist\TaffyDesktopStudio.exe"
Write-Host "verify: dist\TaffyDesktopStudio.exe --self-test"
