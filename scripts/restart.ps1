# Kill leftover uvicorn/python processes and start the app again.
#
# Why: on Windows the uvicorn --reload watcher sometimes reports "Reloading..."
# but the old worker keeps serving the old code (templates still update, routes
# do not). After changing app/*.py, use this script instead of waiting.
$ErrorActionPreference = "Stop"

Get-CimInstance Win32_Process -Filter "Name='python.exe'" | ForEach-Object {
    try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop } catch {}
}
Start-Sleep -Seconds 2

$busy = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
if ($busy) {
    Write-Host "port 8000 is still in use by PID(s):" ($busy | Select-Object -ExpandProperty OwningProcess)
    exit 1
}

& "$PSScriptRoot\run.ps1"
