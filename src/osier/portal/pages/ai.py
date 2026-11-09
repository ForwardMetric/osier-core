"""AI settings: model connections (with their keys), model profiles, and usage.

Keys are typed into a password field, encrypted by the credential broker, and never
shown again: no page renders them, and they are not part of any redirect or log.
"""

from __future__ import annotations

import re
from typing import Any

from starlette.responses import Response

from ...ai import AIService
from ..core import Ctx, NavItem


def _models(text: str) -> list[str]:
    return [m.strip() for m in re.split(r"[\n,]+", text) if m.strip()]


def _service(c: Ctx, engine: Any) -> AIService:
    engine.authz.require("ai.manage", "managing AI models")
    return AIService(engine, c.config.broker())


def register(portal: Any) -> None:
    portal.nav.append(NavItem("AI models", "/ai", ("ai.manage",)))

    @portal.page("/ai")
    def overview(c: Ctx) -> Response:
        with c.engine() as engine:
            status = _service(c, engine).status()
        known = sorted({f"{x['name']}: {m}" for x in status["connections"] for m in x["models"]})
        return c.render(
            "ai.html", ai=status, providers=["openrouter", "openai", "compatible"], known=known
        )

    @portal.page("/ai/connections", methods=("POST",))
    def connect(c: Ctx) -> Response:
        f = c.form
        assert f is not None
        settings: dict[str, Any] = {}
        for name in (
            "input_usd_per_million",
            "output_usd_per_million",
            "monthly_token_limit",
            "monthly_budget_usd",
        ):
            if value := f.number(name):
                settings[name] = value
        with c.engine() as engine:
            service = _service(c, engine)
            result = service.connect(
                f.get("name"),
                provider=f.get("provider"),
                models=_models(f.get("models")),
                api_key=f.raw("api_key").strip() or None,
                api_base=f.get("api_base") or None,
                **settings,
            )
        return c.redirect("/ai", f"Connected {result['name']}.")

    @portal.page("/ai/connections/{name}/update", methods=("POST",))
    def update(c: Ctx) -> Response:
        f = c.form
        assert f is not None
        changes: dict[str, Any] = {}
        if models := _models(f.get("models")):
            changes["models"] = models
        for name in (
            "input_usd_per_million",
            "output_usd_per_million",
            "monthly_token_limit",
            "monthly_budget_usd",
        ):
            if value := f.number(name):
                changes[name] = value
        with c.engine() as engine:
            _service(c, engine).update_connection(c.params["name"], changes)
        return c.redirect("/ai", f"Updated {c.params['name']}.")

    @portal.page("/ai/connections/{name}/disconnect", methods=("GET", "POST"))
    def disconnect(c: Ctx) -> Response:
        name = c.params["name"]
        with c.engine() as engine:
            service = _service(c, engine)
            if c.form is None:
                known = {x["name"] for x in service.connections()}
                if name not in known:
                    return c.render(
                        "error.html",
                        status=404,
                        title="Not found",
                        message=f"No AI connection named {name!r}.",
                        hint=None,
                        expected=None,
                        code="AI_CONNECTION_NOT_FOUND",
                        back="/ai",
                    )
                return c.render("ai_disconnect.html", name=name)
            service.disconnect(name)
        return c.redirect("/ai", f"Removed {name} and its stored key.")

    @portal.page("/ai/profiles", methods=("POST",))
    def profile(c: Ctx) -> Response:
        f = c.form
        assert f is not None
        model = f.get("model")
        if ": " in model:  # picked from the suggestions ("connection: model")
            model = model.split(": ", 1)[1]
        with c.engine() as engine:
            result = _service(c, engine).set_profile(
                name=f.get("name"),
                connection=f.get("connection"),
                model=model,
                effort=f.get("effort") or None,
                description=f.get("description"),
                default=f.checked("default"),
            )
        return c.redirect("/ai", f"Saved profile {result['name']}.")

    @portal.page("/ai/profiles/{name}/remove", methods=("POST",))
    def remove_profile(c: Ctx) -> Response:
        with c.engine() as engine:
            _service(c, engine).remove_profile(c.params["name"])
        return c.redirect("/ai", f"Removed profile {c.params['name']}.")
