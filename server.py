"""Entry point for the MangaShelf web server.

Run:  python web/server.py        (or use run-web.ps1)
Serves the JSON API from api.py and the static React frontend from web/static/.
Bound to 0.0.0.0 so other devices on your home network can reach it at
http://<this-pc-ip>:8000 — open that in your phone's browser.
"""
from __future__ import annotations

import os
import signal
from pathlib import Path

import uvicorn
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from api import app, WEB_TOKEN, _password_is_set

_STATIC_DIR = Path(__file__).resolve().parent / "static"


class NoCacheHTMLMiddleware(BaseHTTPMiddleware):
    """Keep index.html uncacheable so a new bundle version is always picked up.

    The app now ships a single precompiled, minified bundle (app.bundle.js, built
    by build.js) with a static ?v= token, so the JS/CSS themselves cache normally.
    Only index.html must stay no-store: it carries the ?v= token that busts the
    bundle cache on release, so the browser must always re-read it.
    """

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path.endswith(".html") or path == "/":
            response.headers["Cache-Control"] = "no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response


app.add_middleware(NoCacheHTMLMiddleware)


@app.get("/", response_class=HTMLResponse)
@app.get("/index.html", response_class=HTMLResponse)
def index() -> HTMLResponse:
    """Serve index.html with the auth bootstrap injected.

    The bundle is precompiled (see build.js) and cached via its static ?v= token,
    so there's no per-load cache-busting to do here — index.html just injects the
    auth bootstrap and is itself served no-store (see NoCacheHTMLMiddleware).
    """
    html = (_STATIC_DIR / "index.html").read_text(encoding="utf-8")
    # Auth bootstrap. Two modes:
    #  - No password set: inject the per-install token directly so the app "just
    #    works" (original behaviour). A cross-site page can't read it (same-origin
    #    body), so it still doubles as CSRF protection.
    #  - Password set: DO NOT inject the token. The page boots to a login screen
    #    and must POST the password to /api/login to obtain the token. This is what
    #    keeps another device on the same tailnet out — reaching the URL isn't
    #    enough without the password.
    if _password_is_set():
        boot = "window.MANGASHELF_PASSWORD_REQUIRED=true;"
    else:
        boot = f"window.MANGASHELF_TOKEN={_json_str(WEB_TOKEN)};"
    html = html.replace("<head>", f"<head><script>{boot}</script>", 1)
    return HTMLResponse(
        html,
        headers={"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"},
    )


def _json_str(s: str) -> str:
    import json
    return json.dumps(s)


# Mount the SPA last so the custom "/" route and /api/* take precedence.
app.mount("/", StaticFiles(directory=str(_STATIC_DIR), html=True), name="static")


def main() -> None:
    # Bind to localhost by default. Set MANGASHELF_HOST=0.0.0.0 to deliberately
    # expose the app on your LAN (e.g. to read on a phone). Mutating endpoints are
    # token-protected either way, but localhost-only is the safe default so the
    # app isn't reachable by other devices unless you opt in.
    host = os.environ.get("MANGASHELF_HOST", "127.0.0.1")
    port = int(os.environ.get("MANGASHELF_PORT", "8000"))
    if host not in ("127.0.0.1", "localhost"):
        print(f"MangaShelf web — http://localhost:{port}  (LAN: http://<your-pc-ip>:{port})")
    else:
        print(f"MangaShelf web — http://localhost:{port}  (localhost only; set MANGASHELF_HOST=0.0.0.0 for LAN)")
    print("Press Ctrl+C to stop the server.")

    # Configure the server explicitly so shutdown is RELIABLE. With the previous
    # uvicorn.run(app, ...) call, pressing Ctrl+C often didn't actually stop the
    # server: a phone keeps an idle keep-alive connection open, and uvicorn's
    # graceful shutdown waits on it — so the socket never closed and the app
    # stayed reachable. Two changes fix that:
    #   - timeout_graceful_shutdown: don't wait more than a couple seconds for
    #     lingering connections; drop them and release the port.
    #   - a hard signal handler (below) that force-exits if the server is wedged.
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        timeout_graceful_shutdown=2,
    )
    server = uvicorn.Server(config)

    # Belt-and-braces: if a second Ctrl+C arrives (or shutdown stalls), force the
    # process to exit immediately so the port is freed no matter what.
    def _force_quit(signum, frame):
        print("\nForcing shutdown — releasing the port.")
        os._exit(0)

    # First Ctrl+C → uvicorn's own handler asks for a graceful stop. A SECOND one
    # within the grace window hits this and hard-exits. We install it for SIGTERM
    # too (e.g. when the launcher window is closed).
    try:
        signal.signal(signal.SIGTERM, _force_quit)
    except (ValueError, AttributeError, OSError):
        pass  # not available in some environments; the graceful path still works

    server.run()


if __name__ == "__main__":
    main()
