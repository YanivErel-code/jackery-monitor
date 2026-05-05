"""App-level authentication: middleware, /api/auth/* routes, login/setup pages.

Optional layer. The first time the app starts with no /data/auth.json,
a one-time /setup flow lets the operator pick a username/password. After
that, every request must carry a valid session cookie (HMAC-signed) or
it gets a 401 + redirect to /login.

Routes exempt from auth: /login, /setup, /static/*, /manifest.webmanifest,
/sw.js, /api/auth/* (the auth endpoints themselves), and /ws (handled
separately in the WebSocket handler).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

import auth

_AUTH_PUBLIC_PREFIXES = (
    "/static",
    "/manifest.webmanifest",
    "/sw.js",
    "/login",
    "/setup",
    "/api/auth/",
    # Pre-auth restore: lets a fresh install pull data from backup
    # before creating an admin account. Endpoint handlers verify
    # auth.has_user() == False themselves — see _require_no_user().
    "/api/backup/setup_restore/",
)


def set_app_cookie(response: Response, name: str, value: str,
                   max_age: int) -> None:
    """Set a long-lived first-party cookie with the flags we use everywhere
    in this app: HttpOnly, SameSite=Lax, path=/, and Secure=False because
    Cloudflare Tunnel terminates TLS at the edge — the request to our
    origin is HTTP, so Secure would block the cookie."""
    response.set_cookie(
        name, value, max_age=max_age,
        httponly=True, samesite="lax", secure=False, path="/",
    )


def _set_session_cookie(response: Response, username: str) -> None:
    set_app_cookie(response, auth.COOKIE_NAME,
                   auth.make_session(username), auth.SESSION_TTL_S)


def install(app: FastAPI, web_dir: Path) -> None:
    """Register auth middleware, /api/auth/* routes, and login/setup pages."""

    @app.middleware("http")
    async def _auth_middleware(request: Request, call_next):
        path = request.url.path
        if any(path == p or path.startswith(p) for p in _AUTH_PUBLIC_PREFIXES):
            return await call_next(request)
        # First-time-setup gate: no user yet → force /setup
        if not auth.has_user():
            if path.startswith("/api/"):
                return JSONResponse({"detail": "setup_required"}, status_code=401)
            return RedirectResponse("/setup", status_code=303)
        # Auth check
        token = request.cookies.get(auth.COOKIE_NAME)
        payload = auth.verify_session(token)
        if not payload:
            if path.startswith("/api/"):
                return JSONResponse({"detail": "auth_required"}, status_code=401)
            return RedirectResponse("/login", status_code=303)
        return await call_next(request)

    @app.post("/api/auth/setup")
    async def api_auth_setup(body: dict, response: Response):
        """One-time bootstrap: create the admin user if none exists yet."""
        if auth.has_user():
            raise HTTPException(403, "already set up")
        username = ((body or {}).get("username") or "").strip()
        password = (body or {}).get("password") or ""
        if not username or len(password) < 6:
            raise HTTPException(400, "username and password (>=6 chars) required")
        if not auth.save_user(username, password):
            raise HTTPException(500, "failed to save user")
        _set_session_cookie(response, username)
        return {"ok": True, "username": username}

    @app.post("/api/auth/login")
    async def api_auth_login(body: dict, response: Response):
        if not auth.has_user():
            raise HTTPException(403, "setup required")
        username = ((body or {}).get("username") or "").strip()
        password = (body or {}).get("password") or ""
        user = auth.load_user()
        if not user or username != user.get("username") or \
           not auth.verify_password(password, user.get("password_hash", "")):
            raise HTTPException(401, "invalid credentials")
        _set_session_cookie(response, username)
        return {"ok": True, "username": username}

    @app.post("/api/auth/logout")
    async def api_auth_logout(response: Response):
        response.delete_cookie(auth.COOKIE_NAME, path="/")
        return {"ok": True}

    @app.get("/api/auth/me")
    async def api_auth_me(request: Request):
        payload = auth.verify_session(request.cookies.get(auth.COOKIE_NAME))
        if not payload:
            raise HTTPException(401, "auth_required")
        return {"username": payload.get("u")}

    @app.post("/api/auth/change_password")
    async def api_auth_change_password(body: dict, request: Request):
        payload = auth.verify_session(request.cookies.get(auth.COOKIE_NAME))
        if not payload:
            raise HTTPException(401, "auth_required")
        user = auth.load_user()
        if not user:
            raise HTTPException(500, "no user")
        current = (body or {}).get("current") or ""
        new = (body or {}).get("new") or ""
        if not auth.verify_password(current, user.get("password_hash", "")):
            raise HTTPException(401, "current password is wrong")
        if len(new) < 6:
            raise HTTPException(400, "new password too short (>=6 chars)")
        if not auth.save_user(user["username"], new):
            raise HTTPException(500, "failed to save")
        return {"ok": True}

    @app.get("/login")
    def login_page():
        return FileResponse(web_dir / "login.html")

    @app.get("/setup")
    def setup_page():
        return FileResponse(web_dir / "login.html")
