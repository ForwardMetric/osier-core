"""The home page: who you are and what is left to set up."""

from __future__ import annotations

from typing import Any

from starlette.responses import Response

from ...errors import OsierError
from ..core import Ctx, NavItem


def _try(fn: Any) -> Any:
    """A figure for the checklist, or None when this person may not see it."""
    try:
        return fn()
    except OsierError:
        return None


def register(portal: Any) -> None:
    portal.nav.insert(0, NavItem("Home", "/"))

    @portal.page("/")
    def home(c: Ctx) -> Response:
        who = c.who()
        steps: list[dict[str, Any]] = []
        with c.engine() as engine:
            if "ai.manage" in who.permissions:
                from ...ai import AIService

                status = AIService(engine).status()
                steps.append(
                    {
                        "label": "Connect an AI model",
                        "done": bool(status["connections"]) and bool(status["default_profile"]),
                        "detail": f"{len(status['connections'])} connection(s), "
                        f"{len(status['profiles'])} profile(s)",
                        "link": "/ai",
                    }
                )
            if "connections.manage" in who.permissions:
                connections = _try(lambda: len(c.connectors(engine).list_connections()))
                steps.append(
                    {
                        "label": "Connect apps (email, MCP servers)",
                        "done": bool(connections),
                        "detail": f"{connections or 0} connected",
                        "link": "/apps",
                    }
                )
        from .approvals import count

        return c.render("dashboard.html", steps=steps, waiting=count(c))
