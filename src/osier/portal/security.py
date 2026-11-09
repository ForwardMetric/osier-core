"""Portal security: server-side sessions, CSRF tokens, host check, response headers.

The portal can change AI keys and connected apps, so even on localhost it defends
against drive-by attacks from other web pages:

- **Sessions are server-side**; the cookie only carries a random id (HttpOnly,
  SameSite=Lax, Secure when served over https). Sessions expire when idle and after a
  maximum age, and are replaced at login.
- **CSRF**: every POST needs the session's token (a hidden form field) and, when the
  browser sends an Origin header, it must match the portal's own origin.
- **Host check**: requests whose Host header is not one of the allowed hosts are
  refused, which defeats DNS-rebinding attacks against a local server.
- **Headers**: a strict Content-Security-Policy (no scripts, no inline styles, no
  framing), no sniffing, no referrer to other sites, and no caching of pages (forms carry secrets).
"""

from __future__ import annotations

import hmac
import secrets
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

COOKIE = "osier_session"

CSP = (
    "default-src 'none'; style-src 'self'; img-src 'self' data:; form-action 'self'; "
    "base-uri 'none'; frame-ancestors 'none'"
)


@dataclass
class PortalSession:
    id: str
    user_id: uuid.UUID
    csrf: str
    created: float
    last_seen: float
    flash: list[tuple[str, str]] = field(default_factory=list)
    # Multi-step flows keep values (such as a typed-in secret) here, in server memory, so
    # they are never written back into a page.
    pending: dict[str, dict[str, Any]] = field(default_factory=dict)


class SessionStore:
    def __init__(
        self,
        *,
        idle_seconds: float = 2 * 3600,
        absolute_seconds: float = 12 * 3600,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.idle_seconds, self.absolute_seconds, self._clock = (
            idle_seconds,
            absolute_seconds,
            clock,
        )
        self._sessions: dict[str, PortalSession] = {}
        self._lock = threading.Lock()

    def create(self, user_id: uuid.UUID) -> PortalSession:
        now = self._clock()
        session = PortalSession(
            id=secrets.token_urlsafe(32),
            user_id=user_id,
            csrf=secrets.token_urlsafe(32),
            created=now,
            last_seen=now,
        )
        with self._lock:
            self._sweep(now)
            self._sessions[session.id] = session
        return session

    def get(self, session_id: str | None) -> PortalSession | None:
        if not session_id:
            return None
        now = self._clock()
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            if self._expired(session, now):
                del self._sessions[session_id]
                return None
            session.last_seen = now
            return session

    def destroy(self, session_id: str | None) -> None:
        with self._lock:
            self._sessions.pop(session_id or "", None)

    def _expired(self, session: PortalSession, now: float) -> bool:
        return (
            now - session.last_seen > self.idle_seconds
            or now - session.created > self.absolute_seconds
        )

    def _sweep(self, now: float) -> None:
        for sid in [s for s, v in self._sessions.items() if self._expired(v, now)]:
            del self._sessions[sid]


def csrf_ok(session: PortalSession, supplied: str | None) -> bool:
    return bool(supplied) and hmac.compare_digest(session.csrf, supplied or "")


def same_origin(request: Request) -> bool:
    """A browser POST carries an Origin header; if present it must be this site."""
    origin = request.headers.get("origin")
    if origin is None:
        return True  # not a browser form post; the CSRF token still applies
    host = request.headers.get("host", "")
    return origin in (f"http://{host}", f"https://{host}")


class HostCheckMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: Any, allowed_hosts: set[str]) -> None:
        super().__init__(app)
        self.allowed_hosts = allowed_hosts

    async def dispatch(self, request: Request, call_next: Callable[..., Any]) -> Response:
        if request.headers.get("host", "") not in self.allowed_hosts:
            return PlainTextResponse("Unexpected Host header.", status_code=400)
        return await call_next(request)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable[..., Any]) -> Response:
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        # "same-origin", not "no-referrer": with no-referrer, browsers send "Origin: null" on
        # form posts, which the CSRF origin check (rightly) refuses.
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        if not request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        return response
