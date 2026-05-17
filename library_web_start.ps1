# Start the Academic Library web console (PowerShell variant).
# Reads library_web_host / library_web_port from mcp_runtime_config.json when present.

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$host_ = "127.0.0.1"
$port  = 5577
$cfgPath = Join-Path $PSScriptRoot "mcp_runtime_config.json"
if (Test-Path $cfgPath) {
    try {
        $cfg = Get-Content $cfgPath -Raw | ConvertFrom-Json
        if ($cfg.library_web_host) { $host_ = [string]$cfg.library_web_host }
        if ($cfg.library_web_port) { $port  = [int]$cfg.library_web_port }
    } catch {
        Write-Warning "Failed to parse mcp_runtime_config.json: $($_.Exception.Message)"
    }
}

$pyExe = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $pyExe)) {
    Write-Error "Python venv not found at $pyExe. Run: python -m venv .venv; .\.venv\Scripts\pip install -r requirements.txt"
    exit 1
}

$listener = Get-NetTCPConnection -State Listen -LocalAddress $host_ -LocalPort $port -ErrorAction SilentlyContinue
if ($listener) {
    Write-Host "[WARN] Port $port is already in use (PID $($listener.OwningProcess)). Opening browser instead."
    Start-Process "http://${host_}:${port}/"
    exit 0
}

Write-Host "Starting Academic Library web console at http://${host_}:${port}/ ..."
$args = @("-m", "uvicorn", "library_web.app:app", "--host", $host_, "--port", $port, "--log-level", "warning")
Start-Process -FilePath $pyExe -ArgumentList $args -WindowStyle Hidden | Out-Null

Start-Sleep -Milliseconds 1500
Start-Process "http://${host_}:${port}/"
