"""Sign-in for the local portal.

The portal prints a one-time launch link (``/login?key=...``) in the terminal. Opening it
shows the people in this organization to sign in as. Without the key, the page only says
how to get the link, so another web page cannot simply sign itself in.
"""

from __future__ import annotations

import uuid
from typing import Any

from starlette.responses import HTMLResponse, RedirectResponse, Response

from ...context import Principal
from ...session import Session
from ..core import Ctx
from ..security import COOKIE


def _people(portal: Any) -> list[dict[str, Any]]:
    with portal.config.connect() as conn:
        system = Session(conn, portal.config.tenant, Principal.system("portal-login"))
        users = system.records.list("app_user", where={"status": "active"}, limit=200)
    return sorted(users, key=lambda u: u["display_name"].lower())


def register(portal: Any) -> None:
    @portal.page("/login", methods=("GET", "POST"), auth=False)
    def login(c: Ctx) -> Response:
        key = c.request.query_params.get("key") if c.form is None else c.form.get("key")
        render = portal.templates.get_template
        if not portal.key_ok(key):
            return HTMLResponse(render("login_locked.html").render(), status_code=401)
        if c.form is None:
            return HTMLResponse(render("login.html").render(people=_people(portal), key=key))
        try:
            user_id = uuid.UUID(c.form.get("user"))
        except ValueError:
            return HTMLResponse(
                render("login.html").render(
                    people=_people(portal), key=key, problem="Choose who to sign in as."
                ),
                status_code=400,
            )
        if str(user_id) not in {u["id"] for u in _people(portal)}:
            return HTMLResponse(
                render("login.html").render(
                    people=_people(portal), key=key, problem="That person is not active here."
                ),
                status_code=400,
            )
        return portal.start_session(user_id)

    @portal.page("/logout", methods=("POST",))
    def logout(c: Ctx) -> Response:
        portal.store.destroy(c.request.cookies.get(COOKIE))
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(COOKIE, path="/")
        return response
