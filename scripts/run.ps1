# Start the TradeAI web app on Windows.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
& "$root\.venv\python.exe" -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
