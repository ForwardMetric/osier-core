"""The portal application: routing, sign-in, and the machinery pages share.

Every page handler is a plain function taking a ``Ctx``. ``ctx.engine()`` gives an
engine ``Session`` that runs **as the signed-in person** (source ``portal``), so the
engine's own permission checks apply to everything the portal does; the portal also
hides pages the person cannot use, but that is a convenience, not the protection.
"""

from __future__ import annotations

import hmac
import logging
import secrets
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from jinja2 import Environment, PackageLoader
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from ..authz import PermissionDenied
from ..context import Principal
from ..devconfig import DevConfig
from ..errors import ConflictError, NotFoundError, OsierError
from ..session import Session
from .security import (
    COOKIE,
    HostCheckMiddleware,
    PortalSession,
    SecurityHeadersMiddleware,
    SessionStore,
    csrf_ok,
    same_origin,
)

log = logging.getLogger("osier.portal")


class Form:
    """Submitted form fields (text only)."""

    def __init__(self, data: Any) -> None:
        self._items = [(k, v) for k, v in data.multi_items() if isinstance(v, str)]

    def get(self, name: str, default: str = "") -> str:
        values = [v for k, v in self._items if k == name]
        return values[-1].strip() if values else default

    def raw(self, name: str) -> str:
        """The value exactly as typed (for secrets: no trimming of the middle or ends)."""
        values = [v for k, v in self._items if k == name]
        return values[-1] if values else ""

    def getlist(self, name: str) -> list[str]:
        return [v.strip() for k, v in self._items if k == name and v.strip()]

    def checked(self, name: str) -> bool:
        return self.get(name).lower() in ("1", "on", "true", "yes")

    def number(self, name: str) -> str | None:
        value = self.get(name)
        return value or None


@dataclass
class NavItem:
    label: str
    path: str
    any_of: tuple[str, ...] = ()  # shown when the person has one of these permissions


@dataclass
class Who:
    user_id: Any
    name: str
    email: str
    roles: list[str]
    permissions: set[str]


def status_for(exc: OsierError) -> int:
    if isinstance(exc, PermissionDenied):
        return 403
    if isinstance(exc, NotFoundError):
        return 404
    if isinstance(exc, ConflictError):
        return 409
    return 400


class Ctx:
    def __init__(
        self,
        portal: Portal,
        request: Request,
        session: PortalSession | None,
        form: Form | None,
    ) -> None:
        self.portal, self.request, self.session, self.form = portal, request, session, form
        self.params: dict[str, str] = dict(request.path_params)
        self._who: Who | None = None

    @property
    def config(self) -> DevConfig:
        return self.portal.config

    @contextmanager
    def engine(self) -> Iterator[Session]:
        """An engine session running as the signed-in person."""
        assert self.session is not None
        with self.config.connect() as conn:
            principal = Principal(user_id=self.session.user_id, source="portal")
            yield Session(conn, self.config.tenant, principal)

    def connectors(self, engine: Session) -> Any:
        """The connector service for an engine session (credential broker from config)."""
        from ..connectors import ConnectorService

        return ConnectorService(
            engine,
            self.config.broker(),
            allow_private_network=self.config.allow_private_network,
            allow_local_processes=self.config.allow_local_processes,
        )

    def who(self) -> Who:
        if self._who is None:
            assert self.session is not None
            with self.engine() as engine:
                user = engine.records.get("app_user", self.session.user_id)
                self._who = Who(
                    user_id=self.session.user_id,
                    name=user["display_name"],
                    email=user["email"],
                    roles=sorted(engine.authz.roles()),
                    permissions=set(engine.authz.permissions()),
                )
        return self._who

    MAX_HELD = 5

    def hold(self, value: dict[str, Any]) -> str:
        """Keep a value (such as a typed secret or a fetched skill) in server memory until
        the person confirms, and return a token for it. A few at most stay open."""
        assert self.session is not None
        while len(self.session.pending) >= self.MAX_HELD:
            self.session.pending.pop(next(iter(self.session.pending)))
        token = secrets.token_urlsafe(16)
        self.session.pending[token] = value
        return token

    def held(self, token: str) -> dict[str, Any] | None:
        assert self.session is not None
        return self.session.pending.get(token)

    def release(self, token: str) -> None:
        assert self.session is not None
        self.session.pending.pop(token, None)

    def flash(self, message: str, kind: str = "ok") -> None:
        if self.session is not None:
            self.session.flash.append((kind, message))

    def redirect(self, to: str, flash: str | None = None, kind: str = "ok") -> Response:
        if flash:
            self.flash(flash, kind)
        return RedirectResponse(to, status_code=303)

    def render(self, template: str, status: int = 200, **values: Any) -> Response:
        who = self.who()
        assert self.session is not None
        flashes, self.session.flash = list(self.session.flash), []
        html = self.portal.templates.get_template(template).render(
            who=who,
            nav=self.portal.visible_nav(who.permissions),
            csrf=self.session.csrf,
            flashes=flashes,
            current=self.request.url.path,
            **values,
        )
        return HTMLResponse(html, status_code=status)

    def back_path(self) -> str:
        """Where "go back" leads: the page the form came from, if it was this site."""
        from urllib.parse import urlparse

        referer = urlparse(self.request.headers.get("referer", ""))
        if referer.netloc and referer.netloc == self.request.headers.get("host"):
            return referer.path + (f"?{referer.query}" if referer.query else "")
        return "/"

    def error(self, exc: OsierError) -> Response:
        expected = exc.expected if isinstance(exc.expected, list) else None
        values = {
            "title": "That did not work",
            "message": exc.message,
            "hint": exc.hint,
            "expected": [str(e) for e in expected][:30] if expected else None,
            "code": exc.code,
            "back": self.back_path(),
        }
        if self.session is None:
            return HTMLResponse(
                self.portal.templates.get_template("error_public.html").render(**values),
                status_code=status_for(exc),
            )
        return self.render("error.html", status=status_for(exc), **values)


class Portal:
    def __init__(
        self,
        config: DevConfig,
        *,
        launch_key: str | None = None,
        host: str = "127.0.0.1",
        port: int = 8000,
        secure: bool = False,
        store: SessionStore | None = None,
    ) -> None:
        self.config = config
        self.launch_key = launch_key or secrets.token_urlsafe(32)
        self.host, self.port, self.secure = host, port, secure
        self.store = store or SessionStore()
        self.allowed_hosts = {f"{host}:{port}", f"localhost:{port}", f"127.0.0.1:{port}"}
        self.templates = Environment(
            loader=PackageLoader("osier.portal", "templates"), autoescape=True
        )
        self.routes: list[Route | Mount] = []
        self.nav: list[NavItem] = []

    @property
    def login_url(self) -> str:
        return f"http://{self.host}:{self.port}/login?key={self.launch_key}"

    def visible_nav(self, permissions: set[str]) -> list[NavItem]:
        return [n for n in self.nav if not n.any_of or permissions & set(n.any_of)]

    def page(
        self, path: str, *, methods: tuple[str, ...] = ("GET",), auth: bool = True
    ) -> Callable[[Callable[[Ctx], Response]], Callable[[Ctx], Response]]:
        def decorate(handler: Callable[[Ctx], Response]) -> Callable[[Ctx], Response]:
            async def endpoint(request: Request) -> Response:
                session = self.store.get(request.cookies.get(COOKIE))
                if auth and session is None:
                    return RedirectResponse("/login", status_code=303)
                form = None
                if request.method == "POST":
                    if not same_origin(request):
                        return HTMLResponse("Cross-site request refused.", status_code=403)
                    form = Form(await request.form())
                    if session is not None and not csrf_ok(session, form.get("csrf")):
                        ctx = Ctx(self, request, session, form)
                        return ctx.render(
                            "error.html",
                            status=403,
                            title="Form expired",
                            message="Your form could not be verified (it may have expired).",
                            hint="Go back, reload the page, and try again.",
                            expected=None,
                            code="CSRF",
                            back=ctx.back_path(),
                        )
                ctx = Ctx(self, request, session, form)
                try:
                    return await run_in_threadpool(handler, ctx)
                except OsierError as exc:
                    return await run_in_threadpool(ctx.error, exc)

            self.routes.append(Route(path, endpoint, methods=list(methods)))
            return handler

        return decorate

    def app(self) -> Starlette:
        static = str(__import__("pathlib").Path(__file__).parent / "static")

        async def server_error(request: Request, exc: Exception) -> Response:
            log.exception("portal error", exc_info=exc)
            return HTMLResponse(
                "<h1>Something went wrong</h1><p>See the portal's terminal for details.</p>",
                status_code=500,
            )

        return Starlette(
            routes=[*self.routes, Mount("/static", StaticFiles(directory=static), name="static")],
            middleware=[
                Middleware(HostCheckMiddleware, allowed_hosts=self.allowed_hosts),
                Middleware(SecurityHeadersMiddleware),
            ],
            exception_handlers={500: server_error},
        )

    # ---- sign-in -------------------------------------------------------------------

    def key_ok(self, supplied: str | None) -> bool:
        return bool(supplied) and hmac.compare_digest(self.launch_key, supplied or "")

    def start_session(self, user_id: Any) -> Response:
        session = self.store.create(user_id)
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            COOKIE, session.id, httponly=True, samesite="lax", secure=self.secure, path="/"
        )
        return response


def create_app(
    config: DevConfig,
    *,
    launch_key: str | None = None,
    host: str = "127.0.0.1",
    port: int = 8000,
    secure: bool = False,
    store: SessionStore | None = None,
) -> tuple[Starlette, Portal]:
    from .pages import register_all

    portal = Portal(config, launch_key=launch_key, host=host, port=port, secure=secure, store=store)
    register_all(portal)
    return portal.app(), portal
