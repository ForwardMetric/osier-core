"""Portal pages. Each module registers its routes and navigation entries."""

from __future__ import annotations

from typing import Any


def register_all(portal: Any) -> None:
    from . import ai, approvals, apps, auth, dashboard, people, skills, workflows

    auth.register(portal)
    dashboard.register(portal)
    ai.register(portal)
    apps.register(portal)
    approvals.register(portal)
    workflows.register(portal)
    skills.register(portal)
    people.register(portal)
