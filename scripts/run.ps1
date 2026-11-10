# Start the TradeAI web app on Windows.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
# Only watch app/ so installing packages or writing the SQLite file does not
# trigger a reload loop.
& "$root\.venv\python.exe" -m uvicorn app.main:app --reload --reload-dir app --host 127.0.0.1 --port 8000
