"""Starter skills, the /osier menu, and MCP prompts."""

import asyncio

import pytest
import test_mcp_server
from mcp.client import Client
from test_mcp_server import call, tools

from osier.skills import parse_skill_md
from osier.starter_skills import STARTER_SKILLS, install_starter_skills, starter_skill_md

# Reuse the MCP test fixtures (assignment avoids shadowing warnings).
config = test_mcp_server.config
server = test_mcp_server.server

NAMES = [name for name, *_ in STARTER_SKILLS]


def test_every_starter_skill_is_valid_and_documented():
    assert len(set(NAMES)) == len(NAMES)
    for name, title, description, body in STARTER_SKILLS:
        parsed = parse_skill_md(starter_skill_md(name))
        assert parsed["name"] == name and parsed["description"] == description
        assert title and len(body) > 200


def test_new_tenants_get_them_and_edits_survive(session):
    assert {s["name"] for s in session.skills.list()} >= set(NAMES)
    session.skills.set_status("add-leads", "retired")
    edited = starter_skill_md("whats-waiting").replace("# What is waiting", "# Mine")
    session.skills.save(edited)
    system = session.as_principal(type(session.principal).system("test"))
    assert install_starter_skills(system) == []  # nothing re-added, nothing overwritten
    assert "add-leads" not in {s["name"] for s in session.skills.list()}
    assert session.skills.get("whats-waiting")["instructions"].startswith("# Mine")


def test_missing_starter_skills_are_added_on_upgrade(session, conn):
    from psycopg import sql

    versions = sql.Identifier(session.tenant.schema, "meta_skill_versions")
    skills = sql.Identifier(session.tenant.schema, "meta_skills")
    conn.execute(
        sql.SQL("DELETE FROM {} WHERE skill_id IN (SELECT id FROM {} WHERE name = %s)").format(
            versions, skills
        ),
        ("add-leads",),
    )
    conn.execute(sql.SQL("DELETE FROM {} WHERE name = %s").format(skills), ("add-leads",))
    system = session.as_principal(type(session.principal).system("test"))
    assert install_starter_skills(system) == ["add-leads"]


def test_menu_lists_starters_then_own_skills(server, crm):
    crm.skills.save(
        "---\nname: my-checklist\ndescription: Our own onboarding checklist.\n---\n\nDo it.\n"
    )
    menu = call(server, "osier_menu")
    skills = [i["skill"] for i in menu["menu"]]
    assert skills[: len(NAMES)] == NAMES and skills[-1] == "my-checklist"
    assert menu["count"] == len(skills)
    assert all(i["title"] and i["description"] for i in menu["menu"])


def test_prompts_are_listed_and_point_at_the_skill(server):
    async def go():
        async with Client(server) as client:
            listed = {p.name: p for p in (await client.list_prompts()).prompts}
            menu = await client.get_prompt("osier")
            one = await client.get_prompt("add-leads", {"request": "three people from a talk"})
            return listed, menu, one

    listed, menu, one = asyncio.run(go())
    assert {"osier", *NAMES} <= set(listed)
    assert "osier_menu" in menu.messages[0].content.text
    text = one.messages[0].content.text
    assert "'add-leads'" in text and "three people from a talk" in text


def test_instructions_mention_the_menu():
    from osier.mcp_server import INSTRUCTIONS

    assert "osier_menu" in INSTRUCTIONS and "/osier" in INSTRUCTIONS


NOT_TOOLS = {"activity_type", "lead_source", "row_version", "updated_at", "due_date"}


@pytest.mark.parametrize("name", NAMES)
def test_skill_text_names_only_real_tools(server, name):
    import re

    body = {n: b for n, _t, _d, b in STARTER_SKILLS}[name]
    mentioned = set(re.findall(r"`([a-z]+(?:_[a-z]+)+)`", body)) - NOT_TOOLS
    assert mentioned <= set(tools(server)), mentioned - set(tools(server))
