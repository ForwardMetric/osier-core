"""Portal pages. Each module registers its routes and navigation entries."""

from __future__ import annotations

from typing import Any


def register_all(portal: Any) -> None:
    from . import ai, auth, dashboard

    auth.register(portal)
    dashboard.register(portal)
    ai.register(portal)
