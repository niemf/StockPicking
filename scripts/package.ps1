# Build a deployment archive for the Linux server.
#
#   powershell -ExecutionPolicy Bypass -File scripts\package.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\package.ps1 -Output dist\tradeai-v2.tar.gz
#
# Excludes: .venv, .git, data (copy the DB separately), dist, .env, caches.
param(
    [string]$Output = "dist\tradeai.tar.gz"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
    New-Item -ItemType Directory -Force (Split-Path -Parent (Join-Path $root $Output)) | Out-Null
    $target = Join-Path $root $Output
    if (Test-Path $target) { Remove-Item $target -Force }

    tar -czf $target `
        --exclude=.venv `
        --exclude=.git `
        --exclude=.env `
        --exclude=data `
        --exclude=dist `
        --exclude=.pytest_cache `
        --exclude=.ruff_cache `
        --exclude=__pycache__ `
        --exclude=*.pyc `
        --exclude=.idea `
        --exclude=.vscode `
        .

    if ($LASTEXITCODE -ne 0) { throw "tar failed with exit code $LASTEXITCODE" }

    $size = (Get-Item $target).Length / 1MB
    "package: {0} ({1:N1} MB)" -f $target, $size
} finally {
    Pop-Location
}
