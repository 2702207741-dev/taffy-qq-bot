# Download exact, hash-pinned frontend assets. Existing files survive failures.
# Usage: powershell -ExecutionPolicy Bypass -File scripts/vendor.ps1
# Offline: download the URLs manually into frontend\vendor\ and verify hashes.
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$vendorDir = Join-Path $root "frontend\vendor"
New-Item -ItemType Directory -Force -Path $vendorDir | Out-Null

# Version pins per task spec section 3: vendored Vue3 global + ECharts5, no build step.
$packages = @(
    @{ Name = "vue"; Url = "https://unpkg.com/vue@3.4.38/dist/vue.global.prod.js"; File = "vue.global.prod.js"; SHA256 = "b50eeefe35d41636bb96c92b40f1df0b4fb7914e07b3c625b1ec15e9748767b9" },
    @{ Name = "echarts"; Url = "https://unpkg.com/echarts@5.5.1/dist/echarts.min.js"; File = "echarts.min.js"; SHA256 = "e84270bd0cd5bdf60fefc26d00c2a391cb2e81f4d26a7a9ee16185a54773a3cf" }
)

$failed = $false
foreach ($pkg in $packages) {
    $target = Join-Path $vendorDir $pkg.File
    $download = [System.IO.Path]::GetTempFileName()
    try {
        Invoke-WebRequest -Uri $pkg.Url -OutFile $download -UseBasicParsing -TimeoutSec 60
        $hash = (Get-FileHash -LiteralPath $download -Algorithm SHA256).Hash.ToLower()
        if ($hash -ne $pkg.SHA256) { throw "Downloaded asset hash mismatch; existing file retained" }
        Copy-Item -LiteralPath $download -Destination $target -Force
        Write-Host ("[ok] {0} -> {1}" -f $pkg.Name, $target)
        Write-Host ("     sha256 = {0}   # compare against official unpkg artifact" -f $hash)
    }
    catch {
        $failed = $true
        Write-Warning ("Download {0} failed: {1}" -f $pkg.Name, $_.Exception.Message)
        Write-Host ("Manual: fetch {0} and save to {1}" -f $pkg.Url, $target)
        Write-Host "Verify: Get-FileHash -Algorithm SHA256 <file> then compare with upstream release."
    }
    finally {
        # Only the exact temporary file created above; never recursive.
        Remove-Item -LiteralPath $download -ErrorAction SilentlyContinue
    }
}

if ($failed) { exit 1 }
Write-Host "vendor done. self-test step 6 validates presence and size."
