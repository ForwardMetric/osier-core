"""Starter skills: ready-made guides every tenant gets, so people can start from a menu.

They are ordinary text skills (see ``skills.py``): editable, versioned, shareable, and
retire-able. A starter skill is installed once per tenant. If someone edits or retires
it, later upgrades leave it alone. They are also served as MCP prompts (slash commands
in clients that show them) and listed by the ``osier_menu`` tool.
"""

from __future__ import annotations

from typing import Any

from .skills import build_skill_md

# (name, menu title, description, instructions). Descriptions double as the menu text.
STARTER_SKILLS: list[tuple[str, str, str, str]] = [
    (
        "add-leads",
        "Add leads",
        "Capture one or many new sales leads from a pasted list, a note, or a business "
        "card description.",
        """\
# Add leads

Use when the user wants to record people or companies who might become customers.

1. Call `describe_schema` for object `lead` if you have not this session, to see the
   exact field names and allowed values (for example `status`, `lead_source`).
2. Turn what the user gave you into leads. Needed at least: a family name or a company.
   Ask only for what is missing and matters (an email is the most useful extra).
3. Before creating, check for duplicates with `query_records` on `lead` (and `contact`)
   by email. If one exists, tell the user and offer to update it instead.
4. Create each lead with `create_record`. For a list, create them one after another and
   keep a tally.
5. Finish with a short table: name, company, email, and the id of each lead created,
   plus anything skipped and why. Offer next steps: a follow-up email, a task.

Never invent values the user did not give. Leave unknown fields empty.""",
    ),
    (
        "follow-up-on-lead",
        "Follow up with a lead",
        "Write a short, personal follow-up email to a lead and log it, after the user approves.",
        """\
# Follow up with a lead

1. Find the lead: `query_records` on `lead` by name or email. If several match, ask which.
2. Read their record with `get_record` and the recent `activity` records related to
   them, so the message reflects what happened before.
3. Draft a short email (under 120 words): a clear reason for writing, one question or
   next step, no flattery. Show the draft to the user and ask for changes.
4. Only when the user agrees, send it with `send_email`. Say that it may wait for the
   user's approval. Do not retry if it is declined.
5. If the send is accepted, set the lead's `status` to `working` with `update_record`
   (use the `row_version` you just read).

If no email connection exists, say so and point to the portal's Apps page or
`osier connections`. Never ask for a password in chat.""",
    ),
    (
        "pipeline-review",
        "Review my pipeline",
        "Summarize leads and open opportunities: what is new, what is stuck, and what to do next.",
        """\
# Review the pipeline

1. `query_records` on `lead` grouped by what you can count yourself: fetch leads with
   status `new` or `working`, newest first (limit 100).
2. `query_records` on `opportunity` for open ones (use `describe_schema` to see the
   stage values), ordered by close date.
3. Report: counts by status/stage, the five largest open opportunities, anything with
   a close date in the past, and leads untouched for 14+ days (compare `updated_at`).
4. End with three concrete next actions, each naming the record. Offer to create tasks
   (`activity`, type `task`) for them.

State numbers exactly as returned; if a query was truncated by its limit, say so.""",
    ),
    (
        "log-activity",
        "Log a call, meeting, or task",
        "Record a call, meeting, email, or to-do against a lead, contact, account, or opportunity.",
        """\
# Log an activity

1. Identify the record it belongs to (`query_records`); ask if ambiguous.
2. `describe_schema` for `activity` to get the type and status values.
3. `create_record` on `activity` with a clear `subject`, the right `activity_type`,
   the matching lookup field (`lead`, `contact`, `account`, or `opportunity`), and
   `due_date` for tasks. Put what was said or decided in the notes field if present.
4. Confirm what you logged in one line.""",
    ),
    (
        "automate-a-workflow",
        "Automate something",
        "Turn a repeated process into a saved workflow: describe it in plain words, test "
        "it safely, then switch it on.",
        """\
# Automate a workflow

1. Ask what should start it (a person pressing start, a new or changed record, or a
   schedule) and what should happen, in plain words. One round of questions at most.
2. Check `list_workflows` so you do not duplicate one, and `list_connections` to see
   which apps (email, others) are available.
3. Write the definition. Steps are declarative: `get_record`, `query_records`,
   `create_record`, `update_record`, `condition`, `approval` (a person decides),
   `connector` (send email or call an app tool), `transform` (calculate values), and
   `agent` (an AI step; only if an AI connection exists, see `get_ai_status`).
   Put an `approval` step before anything sent outside the organization.
4. Run `test_workflow` with sample inputs. It saves and sends nothing. Show the user
   the step-by-step trace in plain language and fix problems.
5. Save with `save_workflow` (the user confirms), then explain how it starts and where
   approvals appear (`list_pending_approvals`).

Workflows never run code and use the permissions of whoever started them.""",
    ),
    (
        "connect-an-app",
        "Connect an app",
        "Connect email or another app so osier can use it, without sharing passwords in chat.",
        """\
# Connect an app

1. `list_connections` to see what exists.
2. For apps that sign in with a browser (OAuth), call `connect_app`, give the user the
   link it returns, and after they say they are done call `finish_app_connection`.
3. For email (SMTP) or apps that need a key, tell the user to add it in the portal's
   Apps page (`osier portal`) or with the `osier` command line. **Never ask for or
   accept a password, token, or API key in chat**, and tell the user not to paste one.
4. After connecting, `list_connector_tools` for the new app, summarize what it can do,
   and suggest one useful thing to try or automate.""",
    ),
    (
        "design-my-data",
        "Set up my own objects",
        "Design custom objects and fields for your business (for example projects, "
        "properties, or shipments).",
        """\
# Design my data

1. Ask what the user tracks and what they need to know about each thing. Look at
   `describe_schema` first: use existing objects (account, contact, lead, opportunity,
   activity) when they fit instead of duplicating them.
2. Propose a small model: objects, fields, and how they relate (lookups). Names start
   with `c__`, use lowercase and underscores, and every object and field needs a real
   description. Prefer picklists for fixed choices.
3. Show the model as a short list and get agreement.
4. Submit it with `propose_schema_change` (the user confirms in their Claude app).
   Agents cannot approve schema changes; if the result says it is waiting, tell the
   user where to approve it (the portal's Approvals page or `osier approvals`).
5. Offer to add a few sample records so they can see it working.""",
    ),
    (
        "whats-waiting",
        "What needs my attention?",
        "List approvals, drafted emails, and workflow runs that are waiting for a person.",
        """\
# What is waiting

1. `list_pending_approvals` for schema changes, app actions, and workflow approvals.
2. `list_workflow_runs` with status `waiting` or `failed`.
3. Summarize in a short list: what it is, who asked, how long it has waited, and what
   approving would do. Approve or reject with `approve_pending` /
   `decide_workflow_approval` only when the user clearly says so for that item.""",
    ),
]


def starter_skill_md(name: str) -> str:
    for skill_name, _title, description, body in STARTER_SKILLS:
        if skill_name == name:
            return build_skill_md(skill_name, description, body)
    raise KeyError(name)


def menu() -> list[dict[str, str]]:
    """What the /osier menu offers: starter skills, plus the tenant's own."""
    return [
        {"skill": name, "title": title, "description": description}
        for name, title, description, _ in STARTER_SKILLS
    ]


def install_starter_skills(session: Any) -> list[str]:
    """Install missing starter skills (as the system). A skill that exists in any state,
    including retired, is left alone. Returns the names added."""
    added = []
    for name, _title, _description, _body in STARTER_SKILLS:
        if session.skills.exists(name):
            continue
        session.skills.save(starter_skill_md(name), source={"type": "starter"})
        added.append(name)
    return added
