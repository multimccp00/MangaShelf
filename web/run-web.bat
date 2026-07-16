@echo off
cd /d "%~dp0.."

REM Rebuild the frontend bundle if Node is available (so an edited .jsx is picked
REM up automatically). Optional: skipped silently if Node isn't installed — the
REM already-built web\static\app.bundle.js is served either way.
where node >nul 2>nul
if %ERRORLEVEL%==0 (
    echo Rebuilding frontend bundle ...
    pushd "web"
    node build.js
    popd
)

powershell -NoProfile -Command "$p = Start-Process '.venv\Scripts\python.exe' -ArgumentList 'web\server.py' -PassThru -NoNewWindow; Write-Host 'MangaShelf web -- http://localhost:8000'; Write-Host 'Press Enter to stop the server.'; $null = $Host.UI.RawUI.ReadKey('NoEcho,IncludeKeyDown'); Stop-Process -Id $p.Id -Force"
