# Launch MangaShelf for remote access over Tailscale.
#
# Binds the server to this machine's Tailscale IP (the 100.x.y.z address) so the
# app is reachable from your other Tailscale devices (e.g. your phone) but not
# from the regular LAN or the public internet.

$ErrorActionPreference = "Stop"

$root = Split-Path $PSScriptRoot -Parent
$pythonExe = Join-Path $root ".venv\Scripts\python.exe"
$serverPath = Join-Path $PSScriptRoot "server.py"

$tsIp = $null
try {
    $tsCmd = Get-Command tailscale -ErrorAction SilentlyContinue
    if ($tsCmd) {
        $tsIp = (& tailscale ip -4 2>$null | Select-Object -First 1)
        if ($tsIp) { $tsIp = $tsIp.Trim() }
    }
} catch {}

if (-not $tsIp) {
    $tsIp = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -match '^100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.' } |
        Select-Object -First 1 -ExpandProperty IPAddress)
}

if (-not $tsIp) {
    Write-Host ""
    Write-Host "Could not find a Tailscale IP on this machine." -ForegroundColor Yellow
    Write-Host "Make sure Tailscale is installed and connected (tailscale up), then re-run." -ForegroundColor Yellow
    exit 1
}

$tokenPath = Join-Path $env:USERPROFILE ".mangashelf\web_token.txt"
$token = $null
if (Test-Path $tokenPath) { $token = (Get-Content $tokenPath -Raw).Trim() }

$port = 8000
Write-Host ""
Write-Host "MangaShelf - remote access via Tailscale" -ForegroundColor Cyan
Write-Host "  Binding to: ${tsIp}:$port (Tailscale only - not LAN, not public)" -ForegroundColor Gray
Write-Host ""
Write-Host "On your phone (with Tailscale connected), open:" -ForegroundColor Green
Write-Host "  http://${tsIp}:$port/" -ForegroundColor White
if ($token) {
    Write-Host "  (the auth token is delivered automatically when the page loads)" -ForegroundColor DarkGray
}
Write-Host ""

$env:MANGASHELF_HOST = $tsIp
$env:MANGASHELF_PORT = "$port"
if ($args -contains "-NoReload" -or $args -contains "-noreload") {
    $env:MANGASHELF_RELOAD = "0"
    $args = $args | Where-Object { $_ -ne "-NoReload" -and $_ -ne "-noreload" }
} else {
    $env:MANGASHELF_RELOAD = "1"
}

Set-Location $root
& $pythonExe $serverPath @args
