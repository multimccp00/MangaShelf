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

# Everything this launcher starts is torn down again when the window closes (see
# the finally block). Anything that was ALREADY running was started by someone
# else — another app window, a tray icon, a dev shell — so we leave it alone.
$ollamaProc = $null
$serverProc = $null

# Local LLM for the AI import fallback (sources/ai.py probes localhost:11434).
# Optional: if Ollama isn't installed, the AI source just stays unavailable.
$ollama = "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe"
if ((Test-Path $ollama) -and -not (Get-Process ollama -ErrorAction SilentlyContinue)) {
    $ollamaProc = Start-Process $ollama "serve" -WindowStyle Hidden -PassThru
}

if (-not (Test-Up)) {
    # Loopback ONLY: the LAN can't reach the server at all. Phone access flows
    # exclusively through the `tailscale serve` HTTPS proxy, which forwards
    # tailnet traffic to 127.0.0.1 — so Tailscale is the single way in.
    $env:MANGASHELF_HOST = "127.0.0.1"
    # Which local model the AI import fallback uses (see sources/ai.py:_pick_model).
    if (-not $env:MANGASHELF_LLM_MODEL) { $env:MANGASHELF_LLM_MODEL = "qwen2.5-coder" }
    $serverProc = Start-Process -FilePath (Join-Path $root ".venv\Scripts\python.exe") `
        -ArgumentList ('"' + (Join-Path $root "web\server.py") + '"') `
        -WorkingDirectory $root -WindowStyle Hidden -PassThru
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

# The app window needs its OWN browser profile. Without --user-data-dir, a
# already-running Chrome/Edge takes over the URL and the process we launched
# exits instantly — we'd then tear the server down while the window is still
# open. A dedicated profile guarantees the PID we wait on IS the app window.
$profileDir = Join-Path $env:LOCALAPPDATA "MangaShelf\browser"
$browserArgs = "--app=$url", "--user-data-dir=`"$profileDir`""

try {
    if ($chrome) {
        $win = Start-Process $chrome -ArgumentList $browserArgs -PassThru
    } else {
        $win = Start-Process "msedge.exe" -ArgumentList $browserArgs -PassThru
    }

    $win.WaitForExit()

    # Safety net for the handoff case above: a window that "closed" in under two
    # seconds almost certainly never opened, and the real one is running under a
    # different PID. Leave the server up rather than yanking it out from under it.
    if (((Get-Date) - $win.StartTime).TotalSeconds -lt 2) {
        $serverProc = $null
        $ollamaProc = $null
    }
} finally {
    # Closing the window stops what this launcher started — no orphaned server
    # quietly serving stale code until the next reboot. /T because server.py
    # spawns a worker child that must go with it.
    if ($serverProc -and -not $serverProc.HasExited) {
        taskkill /PID $serverProc.Id /T /F 2>&1 | Out-Null
    }
    if ($ollamaProc -and -not $ollamaProc.HasExited) {
        taskkill /PID $ollamaProc.Id /T /F 2>&1 | Out-Null
    }
}
