# Stop the Academic Library web console (PowerShell variant).

$ErrorActionPreference = "Continue"
Set-Location -Path $PSScriptRoot

$host_ = "127.0.0.1"
$port  = 5577
$cfgPath = Join-Path $PSScriptRoot "mcp_runtime_config.json"
if (Test-Path $cfgPath) {
    try {
        $cfg = Get-Content $cfgPath -Raw | ConvertFrom-Json
        if ($cfg.library_web_host) { $host_ = [string]$cfg.library_web_host }
        if ($cfg.library_web_port) { $port  = [int]$cfg.library_web_port }
    } catch {}
}

$listener = Get-NetTCPConnection -State Listen -LocalAddress $host_ -LocalPort $port -ErrorAction SilentlyContinue
if (-not $listener) {
    Write-Host "No listener found on ${host_}:${port}."
    exit 0
}

$pids = $listener.OwningProcess | Sort-Object -Unique
foreach ($pidValue in $pids) {
    try {
        Write-Host "Killing PID $pidValue ..."
        Stop-Process -Id $pidValue -Force
    } catch {
        Write-Warning ("Failed to stop PID {0}: {1}" -f $pidValue, $_.Exception.Message)
    }
}
Write-Host "Library web console stopped."
