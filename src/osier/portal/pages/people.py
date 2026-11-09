"""People and roles: who can sign in and what they may do.

The engine decides who may change this (it needs users.manage and a person). On top of
that the portal refuses the two changes that lock an organization out of itself:
removing the last administrator, and suspending or deactivating yourself.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from starlette.responses import Response

from ...errors import IntegrityError, RecordValidationError
from ..core import Ctx, NavItem

ADMIN = "admin"
STATUSES = ["active", "invited", "suspended", "deactivated"]


PAGE = 200  # the most a single query returns
MAX_PEOPLE = 10_000  # the most the portal pages through


def all_records(engine: Any, obj: str) -> list[dict[str, Any]]:
    """Every record of an object, fetched a page at a time."""
    found: list[dict[str, Any]] = []
    while len(found) < MAX_PEOPLE:
        page = engine.records.query({"object": obj, "limit": PAGE, "offset": len(found)})
        found.extend(page["records"])
        if not page["has_more"]:
            break
    return found


def directory(engine: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    roles = sorted(all_records(engine, "role"), key=lambda r: r["key"])
    key_of = {r["id"]: r["key"] for r in roles}
    held: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for a in all_records(engine, "user_role"):
        if a["role"] in key_of:
            held[a["app_user"]][key_of[a["role"]]] = a
    users = []
    for u in sorted(all_records(engine, "app_user"), key=lambda x: x["display_name"].lower()):
        users.append(
            {
                "id": u["id"],
                "name": u["display_name"],
                "email": u["email"],
                "status": u["status"],
                "row_version": u["row_version"],
                "assignments": held[u["id"]],
                "roles": sorted(held[u["id"]]),
            }
        )
    return users, roles


def other_active_admins(users: list[dict[str, Any]], user_id: str) -> int:
    return sum(
        1 for u in users if u["id"] != user_id and u["status"] == "active" and ADMIN in u["roles"]
    )


def register(portal: Any) -> None:
    portal.nav.append(NavItem("People", "/people", ("users.manage",)))

    def _need(engine: Any) -> None:
        engine.authz.require("users.manage", "managing people")

    def _locked_out(c: Ctx) -> RecordValidationError:
        return RecordValidationError(
            "That would leave the organization without an active administrator.",
            code="LAST_ADMIN",
            hint="Give another person the admin role first.",
        )

    @portal.page("/people")
    def index(c: Ctx) -> Response:
        with c.engine() as engine:
            _need(engine)
            users, roles = directory(engine)
        return c.render(
            "people.html",
            users=users,
            roles=roles,
            statuses=STATUSES,
            me=str(c.session.user_id) if c.session else "",
        )

    @portal.page("/people/new", methods=("POST",))
    def add(c: Ctx) -> Response:
        f = c.form
        assert f is not None
        with c.engine() as engine:
            _need(engine)
            user = engine.records.create(
                "app_user",
                {
                    "display_name": f.get("name"),
                    "email": f.get("email"),
                    "status": f.get("status") or "active",
                    "organization": c.config.organization_id,
                },
            )
            known = {r["key"]: r["id"] for r in all_records(engine, "role")}
            for key in f.getlist("roles"):
                if key in known:
                    engine.records.create("user_role", {"app_user": user["id"], "role": known[key]})
        return c.redirect("/people", f"Added {user['display_name']}.")

    @portal.page("/people/{id}/roles", methods=("POST",))
    def set_roles(c: Ctx) -> Response:
        f = c.form
        assert f is not None
        with c.engine() as engine:
            _need(engine)
            users, roles = directory(engine)
            person = next((u for u in users if u["id"] == c.params["id"]), None)
            if person is None:
                raise RecordValidationError("No such person.", code="USER_NOT_FOUND")
            ids = {r["key"]: r["id"] for r in roles}
            wanted = {k for k in f.getlist("roles") if k in ids}
            have = set(person["roles"])
            if (
                ADMIN in have - wanted
                and person["status"] == "active"
                and other_active_admins(users, person["id"]) == 0
            ):
                raise _locked_out(c)
            try:
                with engine.conn.transaction():  # all of the changes, or none
                    for key in sorted(wanted - have):
                        engine.records.create(
                            "user_role", {"app_user": person["id"], "role": ids[key]}
                        )
                    for key in sorted(have - wanted):
                        held = person["assignments"][key]
                        engine.records.retire(
                            "user_role", held["id"], expected_row_version=held["row_version"]
                        )
            except IntegrityError as exc:
                if exc.code != "UNIQUE_VIOLATION":
                    raise
                raise RecordValidationError(
                    "A role that was removed from this person earlier cannot be granted again yet.",
                    code="ROLE_PREVIOUSLY_REMOVED",
                    hint="Known limitation: removed assignments still occupy their slot. "
                    "Nothing was changed.",
                ) from None
        return c.redirect("/people", f"Updated roles for {person['name']}.")

    @portal.page("/people/{id}/status", methods=("POST",))
    def set_status(c: Ctx) -> Response:
        f = c.form
        assert f is not None
        status = f.get("status")
        if status not in STATUSES:
            raise RecordValidationError("Unknown status.", field="status", expected=STATUSES)
        with c.engine() as engine:
            _need(engine)
            users, _ = directory(engine)
            person = next((u for u in users if u["id"] == c.params["id"]), None)
            if person is None:
                raise RecordValidationError("No such person.", code="USER_NOT_FOUND")
            leaving = status != "active" and person["status"] == "active"
            if leaving and person["id"] == str(c.session.user_id if c.session else ""):
                raise RecordValidationError(
                    "You cannot suspend or deactivate yourself here.",
                    code="CANNOT_LOCK_YOURSELF_OUT",
                    hint="Ask another administrator.",
                )
            if (
                leaving
                and ADMIN in person["roles"]
                and other_active_admins(users, person["id"]) == 0
            ):
                raise _locked_out(c)
            engine.records.update(
                "app_user",
                person["id"],
                {"status": status},
                expected_row_version=person["row_version"],
            )
        return c.redirect("/people", f"{person['name']} is now {status}.")
