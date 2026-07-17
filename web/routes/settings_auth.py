"""App settings + the auth API (login / auth-status / password) + health.

Split out of api.py; the password/token primitives stay there (they're used by
the auth dependencies every route needs). See routes/__init__ for the import
contract.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import api
from api import require_token, require_token_read

router = APIRouter()


@router.get("/api/settings", dependencies=[Depends(require_token_read)])
def get_settings() -> dict[str, Any]:
    """App preferences, server-stored so they persist across browser-storage
    clears and sync between devices."""
    return api._load_settings()


@router.post("/api/settings", dependencies=[Depends(require_token)])
def update_settings(body: dict[str, Any]) -> dict[str, Any]:
    """Merge in changed preference keys (unknown keys ignored)."""
    return api._save_settings(body or {})


# ----------------------------------------------------------------- auth API --
class LoginIn(BaseModel):
    password: str


class SetPasswordIn(BaseModel):
    # current is required only when a password is ALREADY set (to change it).
    current: str | None = None
    new: str  # empty string clears the password (reverts to open-on-tailnet)


@router.get("/api/auth-status")
def auth_status() -> JSONResponse:
    """Whether a password gate is configured. No auth required — the login screen
    calls this before the user has any credential."""
    return JSONResponse({"password_set": api._password_is_set()})


@router.post("/api/login")
def login(body: LoginIn) -> JSONResponse:
    """Exchange the app password for the API token. No token required (this is how
    you obtain it). The password check uses PBKDF2 (deliberately slow) + a
    constant-time compare, so it's inherently resistant to timing attacks."""
    if not api._password_is_set():
        # No password configured → login is meaningless; hand back the token so a
        # misconfigured client still works. (Normally the page just gets the token
        # injected directly in this case.)
        return JSONResponse({"ok": True, "token": api.WEB_TOKEN})
    if not api._password_matches(body.password or ""):
        raise HTTPException(status_code=401, detail="Incorrect password")
    return JSONResponse({"ok": True, "token": api.WEB_TOKEN})


@router.post("/api/password", dependencies=[Depends(require_token)])
def set_password(body: SetPasswordIn) -> JSONResponse:
    """Set, change, or clear the app password. Requires the token (you must be
    logged in). If a password is already set, `current` must match it."""
    if api._password_is_set():
        if not api._password_matches(body.current or ""):
            raise HTTPException(status_code=403, detail="Current password is incorrect")
    new = (body.new or "").strip()
    if new and len(new) < 4:
        raise HTTPException(status_code=400, detail="Password must be at least 4 characters")
    api._set_password(new)
    return JSONResponse({"ok": True, "password_set": bool(new)})


@router.get("/api/health")
def health() -> JSONResponse:
    return JSONResponse({"ok": True, "libraries": len(api._library_roots())})
