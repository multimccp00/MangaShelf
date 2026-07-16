# MangaShelf desktop launcher.
# Starts the web server if it isn't already running, waits until it answers,
# then opens the app in its own chromeless window (Chrome/Edge --app mode) —
# so the desktop icon behaves like a real installed app.
#
# Paths are resolved relative to this script (web/), so the project can move.

$root = Split-Path $PSScriptRoot -Parent
$url = "http://localhost:8000"

function Test-Up {
    try {
        (Invoke-WebRequest -Uri "$url/api/health" -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200
    } catch { $false }
}

if (-not (Test-Up)) {
    # Loopback ONLY: the LAN can't reach the server at all. Phone access flows
    # exclusively through the `tailscale serve` HTTPS proxy, which forwards
    # tailnet traffic to 127.0.0.1 — so Tailscale is the single way in.
    $env:MANGASHELF_HOST = "127.0.0.1"
    Start-Process -FilePath (Join-Path $root ".venv\Scripts\python.exe") `
        -ArgumentList ('"' + (Join-Path $root "web\server.py") + '"') `
        -WorkingDirectory $root -WindowStyle Hidden
    # Wait up to ~20s for the server to come up (first start scans the library).
    for ($i = 0; $i -lt 40; $i++) {
        if (Test-Up) { break }
        Start-Sleep -Milliseconds 500
    }
}

# Prefer Chrome; fall back to Edge (always present on Windows 11).
$chrome = @(
    "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
    "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1

if ($chrome) {
    Start-Process $chrome "--app=$url"
} else {
    Start-Process "msedge.exe" "--app=$url"
}
