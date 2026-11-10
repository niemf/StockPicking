# Run the Day-1 data source validation suite.
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
$python = "$root\.venv\python.exe"

Write-Host "`n=== 1/3 Real-time quotes ===" -ForegroundColor Cyan
& $python "$root\scripts\verify_quotes.py"

Write-Host "`n=== 2/3 AkShare ===" -ForegroundColor Cyan
& $python "$root\scripts\verify_akshare.py"

Write-Host "`n=== 3/3 Feishu (optional) ===" -ForegroundColor Cyan
& $python "$root\scripts\verify_feishu.py"
