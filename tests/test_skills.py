"""Text skills in the Claude-compatible Agent Skills format, per tenant."""

import httpx2
import pytest
from conftest import agent_for

from osier.authz import PermissionDenied
from osier.errors import ApprovalError, NotFoundError, RecordValidationError
from osier.skills import SkillError, SkillService, build_skill_md, parse_skill_md

QUALIFY = """---
name: qualify-lead
description: Decide whether a new lead is worth pursuing, using budget, need, and timing.
license: Apache-2.0
---

# Qualify a lead

1. Read the lead and its company.
2. Score budget, need, and timing from 1 to 5.
3. Set status to qualified if the total is 10 or more.
"""


def test_parse_valid_skill():
    parsed = parse_skill_md(QUALIFY)
    assert parsed["name"] == "qualify-lead" and parsed["license"] == "Apache-2.0"
    assert parsed["body"].startswith("# Qualify a lead")


@pytest.mark.parametrize(
    ("text", "field"),
    [
        ("no frontmatter here", None),
        ("---\nname: Qualify Lead\ndescription: x\n---\nbody", "name"),
        ("---\nname: -bad\ndescription: x\n---\nbody", "name"),
        ("---\nname: a--b\ndescription: x\n---\nbody", "name"),
        ("---\nname: " + "a" * 65 + "\ndescription: x\n---\nbody", "name"),
        ("---\nname: ok\n---\nbody", "description"),
        ("---\nname: ok\ndescription: " + "d" * 1025 + "\n---\nbody", "description"),
        ("---\nname: ok\ndescription: fine\n---\n", "body"),
        ("---\nname: ok\ndescription: fine\ncolor: red\n---\nbody", "frontmatter"),
        ("---\nname: ok\ndescription: fine\nmetadata:\n  n: 1\n---\nbody", "metadata"),
    ],
)
def test_invalid_skills(text, field):
    with pytest.raises(SkillError) as exc:
        parse_skill_md(text)
    if field:
        assert exc.value.field == field


def test_create_get_list_and_versions(crm):
    saved = crm.skills.save(QUALIFY)
    assert saved["version"] == 1 and saved["visibility"] == "organization"
    skill = crm.skills.get("qualify-lead")
    assert "Score budget" in skill["instructions"]
    assert parse_skill_md(skill["skill_md"])["name"] == "qualify-lead"  # round-trips

    updated = QUALIFY.replace("10 or more", "11 or more")
    assert crm.skills.save(updated)["version"] == 2
    assert [v["change"] for v in crm.skills.versions("qualify-lead")] == ["created", "updated"]
    assert [s["name"] for s in crm.skills.list()] == ["qualify-lead"]


def test_build_from_fields():
    md = build_skill_md(
        "follow-up",
        "How to write a follow-up email after a first call.",
        "Keep it under 120 words.",
    )
    assert parse_skill_md(md)["description"].startswith("How to write")


def test_members_use_skills_but_cannot_manage_them(crm, make_user):
    crm.skills.save(QUALIFY)
    sam = crm.as_principal(make_user("sam@acme.test", "member"))
    assert sam.skills.get("qualify-lead")["name"] == "qualify-lead"
    with pytest.raises(PermissionDenied) as exc:
        sam.skills.save(QUALIFY)
    assert exc.value.details["permission"] == "skills.manage"


def test_agents_cannot_save_skills_directly(crm):
    agent = crm.as_principal(agent_for(crm.principal.user_id))
    with pytest.raises(ApprovalError):
        agent.skills.save(QUALIFY)
    assert agent.skills.preview(QUALIFY)["action"] == "create"  # previews are fine


def test_visibility(crm, make_user):
    crm.records.create("role", {"name": "Sales Rep", "key": "sales_rep"})
    crm.skills.save(QUALIFY, visibility="roles", visible_roles=["sales_rep"])
    rep = crm.as_principal(make_user("rae@acme.test", "member", "sales_rep"))
    other = crm.as_principal(make_user("ola@acme.test", "member"))
    assert [s["name"] for s in rep.skills.list()] == ["qualify-lead"]
    assert other.skills.list() == []
    with pytest.raises(NotFoundError):
        other.skills.get("qualify-lead")
    crm.skills.share("qualify-lead", "organization")
    assert [s["name"] for s in other.skills.list()] == ["qualify-lead"]
    with pytest.raises(RecordValidationError):
        crm.skills.share("qualify-lead", "roles", ["nope"])


def test_disable_and_retire(crm, make_user):
    crm.skills.save(QUALIFY)
    sam = crm.as_principal(make_user("sam@acme.test", "member"))
    crm.skills.set_status("qualify-lead", "disabled")
    assert sam.skills.list() == []
    crm.skills.set_status("qualify-lead", "active")
    crm.skills.set_status("qualify-lead", "retired")
    assert crm.skills.list() == []
    restored = crm.skills.save(QUALIFY)  # saving again restores it as a new version
    assert restored["version"] == 2 and restored["status"] == "active"


def _github(files, skill_md=QUALIFY):
    def handle(request):
        url = str(request.url)
        if url.startswith("https://api.github.com/repos/acme/skills/contents/qualify-lead"):
            return httpx2.Response(200, json=[{"name": n, "type": t} for n, t in files])
        if url == "https://raw.githubusercontent.com/acme/skills/main/qualify-lead/SKILL.md":
            return httpx2.Response(200, text=skill_md)
        return httpx2.Response(404)

    return httpx2.Client(transport=httpx2.MockTransport(handle))


def test_import_text_skill_from_github(crm):
    svc = SkillService(crm, http_client=_github([("SKILL.md", "file"), ("examples.md", "file")]))
    fetched = svc.fetch("https://github.com/acme/skills/tree/main/qualify-lead")
    assert fetched["preview"]["name"] == "qualify-lead"
    assert fetched["skipped_files"] == ["examples.md"]
    saved = svc.save(fetched["skill_md"], source=fetched["source"])
    assert saved["source"]["url"].endswith("/qualify-lead") and saved["license"] == "Apache-2.0"


def test_skills_with_scripts_are_refused(crm):
    svc = SkillService(crm, http_client=_github([("SKILL.md", "file"), ("scripts", "dir")]))
    with pytest.raises(SkillError) as exc:
        svc.fetch("https://github.com/acme/skills/tree/main/qualify-lead")
    assert exc.value.code == "SKILL_HAS_SCRIPTS"
    svc = SkillService(crm, http_client=_github([("SKILL.md", "file"), ("fill_form.py", "file")]))
    with pytest.raises(SkillError):
        svc.fetch("https://github.com/acme/skills/blob/main/qualify-lead/SKILL.md")


def test_import_requires_https(crm):
    with pytest.raises(SkillError) as exc:
        crm.skills.fetch("http://example.com/SKILL.md")
    assert exc.value.code == "INSECURE_TRANSPORT"


def test_existing_tenants_get_new_permissions_once(crm, conn):
    from psycopg import sql

    from osier.tenant import upgrade_engine_tables

    table = sql.Identifier(crm.tenant.schema, "meta_role_permissions")
    conn.execute(
        sql.SQL("DELETE FROM {} WHERE role_key = 'developer' AND permission = %s").format(table),
        ("skills.manage",),
    )
    upgrade_engine_tables(conn, crm.tenant)  # migration already recorded: not re-granted
    rows = conn.execute(
        sql.SQL("SELECT 1 FROM {} WHERE role_key = 'developer' AND permission = %s").format(table),
        ("skills.manage",),
    ).fetchall()
    assert rows == []


def test_managers_can_review_disabled_skills(session):
    session.skills.save(QUALIFY)
    session.skills.set_status("qualify-lead", "disabled")
    with pytest.raises(NotFoundError) as err:
        session.skills.get("qualify-lead")
    assert err.value.code == "SKILL_DISABLED"
    assert session.skills.get("qualify-lead", include_inactive=True)["status"] == "disabled"
