# Download frontend vendor libs and print sha256 (human-verifiable).
# Usage: powershell -ExecutionPolicy Bypass -File scripts/vendor.ps1
# Offline: download the URLs manually into frontend\vendor\ and verify hashes.
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$vendorDir = Join-Path $root "frontend\vendor"
New-Item -ItemType Directory -Force -Path $vendorDir | Out-Null

# Version pins per task spec section 3: vendored Vue3 global + ECharts5, no build step.
$packages = @(
    @{ Name = "vue"; Url = "https://unpkg.com/vue@3.4.38/dist/vue.global.prod.js"; File = "vue.global.prod.js" },
    @{ Name = "echarts"; Url = "https://unpkg.com/echarts@5.5.1/dist/echarts.min.js"; File = "echarts.min.js" }
)

$failed = $false
foreach ($pkg in $packages) {
    $target = Join-Path $vendorDir $pkg.File
    try {
        Invoke-WebRequest -Uri $pkg.Url -OutFile $target -UseBasicParsing -TimeoutSec 60
        $hash = (Get-FileHash -Path $target -Algorithm SHA256).Hash.ToLower()
        Write-Host ("[ok] {0} -> {1}" -f $pkg.Name, $target)
        Write-Host ("     sha256 = {0}   # compare against official unpkg artifact" -f $hash)
    }
    catch {
        $failed = $true
        Write-Warning ("Download {0} failed: {1}" -f $pkg.Name, $_.Exception.Message)
        Write-Host ("Manual: fetch {0} and save to {1}" -f $pkg.Url, $target)
        Write-Host "Verify: Get-FileHash -Algorithm SHA256 <file> then compare with upstream release."
    }
}

if ($failed) { exit 1 }
Write-Host "vendor done. self-test step 6 validates presence and size."
