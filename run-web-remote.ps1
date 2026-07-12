# Launch MangaShelf for remote access over Tailscale.
#
# Binds the server to this machine's Tailscale IP (the 100.x.y.z address) so the
# app is reachable from your other Tailscale devices (e.g. your phone) but NOT
# from the regular LAN or the public internet. Prints the exact URL — including
# the auth token — to open on your phone.
#
# Prerequisites (one-time):
#   1. Install Tailscale on this PC:    https://tailscale.com/download
#   2. Install Tailscale on your phone (App Store / Play Store) and sign in to
#      the SAME account.
#   3. Run `tailscale up` here if you haven't already.
#
# Then just run this script. No router/firewall/port-forwarding changes needed —
# Tailscale tunnels the connection.

$ErrorActionPreference = "Stop"

# --- find the Tailscale IPv4 (100.64.0.0/10 CGNAT range Tailscale uses) ---
$tsIp = $null
try {
    $tsCmd = Get-Command tailscale -ErrorAction SilentlyContinue
    if ($tsCmd) {
        $tsIp = (& tailscale ip -4 2>$null | Select-Object -First 1)
        if ($tsIp) { $tsIp = $tsIp.Trim() }
    }
} catch {}

if (-not $tsIp) {
    # Fallback: scan local IPv4 addresses for one in the Tailscale range.
    $tsIp = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -match '^100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.' } |
        Select-Object -First 1 -ExpandProperty IPAddress)
}

if (-not $tsIp) {
    Write-Host ""
    Write-Host "Could not find a Tailscale IP on this machine." -ForegroundColor Yellow
    Write-Host "Make sure Tailscale is installed and connected (`tailscale up`), then re-run." -ForegroundColor Yellow
    Write-Host "If you understand the risk and want plain LAN exposure instead, run:" -ForegroundColor Yellow
    Write-Host '    $env:MANGASHELF_HOST = "0.0.0.0"; .\web\run-web.ps1' -ForegroundColor DarkGray
    exit 1
}

# --- read the per-install auth token so we can print the ready-to-use URL ---
$tokenPath = Join-Path $env:USERPROFILE ".mangashelf\web_token.txt"
$token = $null
if (Test-Path $tokenPath) { $token = (Get-Content $tokenPath -Raw).Trim() }

$port = 8000
Write-Host ""
Write-Host "MangaShelf — remote access via Tailscale" -ForegroundColor Cyan
Write-Host "  Binding to: $tsIp`:$port  (Tailscale only — not LAN, not public)" -ForegroundColor Gray
Write-Host ""
Write-Host "On your phone (with Tailscale connected), open:" -ForegroundColor Green
if ($token) {
    Write-Host "  http://$tsIp`:$port/" -ForegroundColor White
    Write-Host "  (the auth token is delivered automatically when the page loads)" -ForegroundColor DarkGray
} else {
    Write-Host "  http://$tsIp`:$port/" -ForegroundColor White
}
Write-Host ""

$env:MANGASHELF_HOST = $tsIp
$env:MANGASHELF_PORT = "$port"
& "$PSScriptRoot\..\.venv\Scripts\python.exe" "$PSScriptRoot\server.py" @args
