"""Portal: skills."""

import re

from conftest import own_skills
from portal_helpers import post, sign_in

SKILL = """---
name: qualify-lead
description: How we qualify leads. Use when a new lead arrives.
license: MIT
---

# Qualify a lead

Check company size, then set the status.
"""


def token_of(html):
    return re.search(r'name="token" value="([^"]+)"', html).group(1)


def test_members_cannot_open_skills(client, make_user):
    sign_in(client, make_user("rep@acme.test", "member").user_id)
    assert client.get("/skills").status_code == 403
    assert post(client, "/skills/new", skill_md=SKILL).status_code == 403


def test_a_skill_is_previewed_in_full_before_it_is_saved(admin_client, crm):
    page = post(admin_client, "/skills/new", skill_md=SKILL)
    assert page.status_code == 200
    assert "Check company size" in page.text and "Licence: MIT" in page.text
    assert own_skills(crm) == []  # nothing saved by previewing
    done = post(admin_client, "/skills/save", token=token_of(page.text), visibility="organization")
    assert done.status_code == 303
    assert [s["name"] for s in own_skills(crm)] == ["qualify-lead"]
    detail = admin_client.get("/skills/qualify-lead")
    assert detail.status_code == 200 and "Check company size" in detail.text
    assert "qualify-lead" in admin_client.get("/skills").text


def test_an_expired_preview_saves_nothing(admin_client, crm):
    response = post(admin_client, "/skills/save", token="nope")
    assert response.status_code == 404 and "expired" in response.text
    assert own_skills(crm) == []


def test_an_invalid_skill_is_explained(admin_client):
    response = post(admin_client, "/skills/new", skill_md="no frontmatter here")
    assert response.status_code == 400


def test_skill_text_is_escaped(admin_client):
    evil = SKILL.replace("Check company size", "<script>alert(1)</script>")
    page = post(admin_client, "/skills/new", skill_md=evil)
    assert "<script>alert(1)" not in page.text and "&lt;script&gt;" in page.text


def test_updating_creates_a_new_version(admin_client, crm):
    for text in (SKILL, SKILL.replace("set the status", "set the status and owner")):
        page = post(admin_client, "/skills/new", skill_md=text)
        post(admin_client, "/skills/save", token=token_of(page.text))
    assert own_skills(crm)[0]["version"] == 2
    assert "Replaces version 2" in post(admin_client, "/skills/new", skill_md=SKILL).text


def test_status_and_sharing(admin_client, crm):
    page = post(admin_client, "/skills/new", skill_md=SKILL)
    post(admin_client, "/skills/save", token=token_of(page.text))
    assert post(admin_client, "/skills/qualify-lead/status", status="disabled").status_code == 303
    assert own_skills(crm)[0]["status"] == "disabled"
    assert (
        post(
            admin_client, "/skills/qualify-lead/share", visibility="roles", visible_roles="member"
        ).status_code
        == 303
    )
    assert own_skills(crm)[0]["visible_roles"] == ["member"]
    assert post(admin_client, "/skills/qualify-lead/status", status="retired").status_code == 303
    assert own_skills(crm) == []


def test_import_from_a_link_previews_with_the_licence(admin_client, monkeypatch):
    import osier.skills as skills_module

    monkeypatch.setattr(
        skills_module.SkillService,
        "fetch",
        lambda self, url: {
            "skill_md": SKILL,
            "source": {"type": "url", "url": url},
            "skipped_files": ["scripts/run.py"],
            "preview": self.preview(SKILL),
        },
    )
    page = post(admin_client, "/skills/new", url="https://example.com/SKILL.md")
    assert page.status_code == 200
    assert "Licence: MIT" in page.text and "scripts/run.py" in page.text
    assert "https://example.com/SKILL.md" in page.text
