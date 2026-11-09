"""Skills: the organization's library of instructions (text skills).

Adding or importing a skill shows the full text (and the licence, for imports) first; it
is saved only when the person confirms. Imported skills are third-party text that agents
will read as instructions, so they are previewed in full, and skills with scripts are
refused by the engine.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from starlette.responses import Response

from ...errors import NotFoundError
from ...skills import SkillService
from ..core import Ctx, NavItem


def _service(c: Ctx, engine: Any) -> SkillService:
    return SkillService(engine, allow_private_network=c.config.allow_private_network)


def _roles(engine: Any) -> list[str]:
    return sorted(r["key"] for r in engine.records.list("role", limit=100))


def register(portal: Any) -> None:
    portal.nav.append(NavItem("Skills", "/skills", ("skills.manage",)))

    @portal.page("/skills")
    def index(c: Ctx) -> Response:
        with c.engine() as engine:
            engine.authz.require("skills.manage", "managing skills")
            return c.render("skills.html", skills=_service(c, engine).list())

    @portal.page("/skills/new", methods=("POST",))
    def preview(c: Ctx) -> Response:
        f = c.form
        assert f is not None
        with c.engine() as engine:
            service = _service(c, engine)
            if f.get("url"):
                fetched = service.fetch(f.get("url"))
                held = {
                    "skill_md": fetched["skill_md"],
                    "source": fetched["source"],
                    "skipped": fetched["skipped_files"],
                    "origin": f.get("url"),
                }
                shown = fetched["preview"]
            else:
                text = f.raw("skill_md")
                shown = service.preview(text)
                held = {"skill_md": text, "source": None, "skipped": [], "origin": None}
            roles = _roles(engine)
        return c.render(
            "skills_confirm.html", token=c.hold(held), shown=shown, held=held, roles=roles
        )

    @portal.page("/skills/save", methods=("POST",))
    def save(c: Ctx) -> Response:
        f = c.form
        assert f is not None
        held = c.held(f.get("token"))
        if held is None:
            raise NotFoundError(
                "That preview expired.",
                code="REVIEW_EXPIRED",
                hint="Start again from Skills; nothing was saved.",
            )
        with c.engine() as engine:
            result = _service(c, engine).save(
                held["skill_md"],
                visibility=f.get("visibility") or "organization",
                visible_roles=f.getlist("visible_roles") or None,
                source=held["source"],
            )
        c.release(f.get("token"))
        return c.redirect(
            f"/skills/{quote(result['name'], safe='')}",
            f"Saved {result['name']} (version {result['version']}).",
        )

    @portal.page("/skills/{name}")
    def detail(c: Ctx) -> Response:
        name = c.params["name"]
        with c.engine() as engine:
            engine.authz.require("skills.manage", "managing skills")
            service = _service(c, engine)
            skill = service.get(name, include_inactive=True)
            return c.render(
                "skills_detail.html",
                skill=skill,
                versions=service.versions(name),
                roles=_roles(engine),
            )

    @portal.page("/skills/{name}/status", methods=("POST",))
    def status(c: Ctx) -> Response:
        f = c.form
        assert f is not None
        name = c.params["name"]
        with c.engine() as engine:
            _service(c, engine).set_status(name, f.get("status"))
        return c.redirect(
            f"/skills/{quote(name, safe='')}" if f.get("status") != "retired" else "/skills",
            f"{name} is now {f.get('status')}.",
        )

    @portal.page("/skills/{name}/share", methods=("POST",))
    def share(c: Ctx) -> Response:
        f = c.form
        assert f is not None
        name = c.params["name"]
        with c.engine() as engine:
            _service(c, engine).share(
                name, f.get("visibility") or "organization", f.getlist("visible_roles") or None
            )
        return c.redirect(f"/skills/{quote(name, safe='')}", "Sharing updated.")
