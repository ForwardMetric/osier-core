"""The app gallery: one tile per kind of connection.

Each tile knows only what a person should not have to type: servers, ports, and
addresses. Entries here are limited to details that providers document publicly; the
portal never claims a provider works until a person has connected it. No third-party
logos are used (a coloured monogram stands in), so nothing here implies endorsement.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Tile:
    key: str
    name: str
    category: str
    blurb: str
    kind: str  # email_preset | email | oauth | mcp | outbox
    color: str  # css class c1..c6
    description: str = ""  # what agents are told the app is for
    settings: dict[str, object] = field(default_factory=dict)
    help: str = ""
    help_url: str = ""
    credential_label: str = "Password"

    @property
    def monogram(self) -> str:
        return self.name[0].upper()


CATEGORIES = ["Email", "Knowledge", "Automation", "Custom", "Developer"]

TILES: list[Tile] = [
    Tile(
        "gmail",
        "Gmail",
        "Email",
        "Send follow-ups from your Gmail address.",
        "email_preset",
        "c1",
        description="Sends email from a Gmail account.",
        settings={"host": "smtp.gmail.com", "port": 587, "security": "starttls"},
        help="Gmail needs an app password, not your normal password. Turn on 2-Step "
        "Verification, then create an app password for osier.",
        help_url="https://myaccount.google.com/apppasswords",
        credential_label="App password",
    ),
    Tile(
        "sendgrid",
        "SendGrid",
        "Email",
        "Send from a SendGrid account (good for volume).",
        "email_preset",
        "c2",
        description="Sends email through SendGrid.",
        settings={
            "host": "smtp.sendgrid.net",
            "port": 587,
            "security": "starttls",
            "username": "apikey",
        },
        help="Use a SendGrid API key as the password. The From address must be a sender "
        "you have verified in SendGrid.",
        credential_label="API key",
    ),
    Tile(
        "smtp",
        "Other email",
        "Email",
        "Any email service that offers SMTP.",
        "email",
        "c3",
    ),
    Tile(
        "notion",
        "Notion",
        "Knowledge",
        "Let agents search and update your Notion workspace.",
        "oauth",
        "c4",
        description="Company wiki, meeting notes and documents in Notion.",
        settings={"url": "https://mcp.notion.com/mcp"},
    ),
    Tile(
        "zapier",
        "Zapier",
        "Automation",
        "Reach thousands of other apps through your Zapier account.",
        "oauth",
        "c5",
        description="Actions in other apps, run through Zapier.",
        settings={"url": "https://mcp.zapier.com/api/v1/connect"},
    ),
    Tile(
        "mcp",
        "Custom app (MCP)",
        "Custom",
        "Connect any server that speaks MCP. You review its tools first.",
        "mcp",
        "c6",
    ),
    Tile(
        "outbox",
        "Development outbox",
        "Developer",
        "Try email without sending anything: messages go to a local log.",
        "outbox",
        "c3",
    ),
]

BY_KEY = {t.key: t for t in TILES}


def search(query: str = "", category: str = "") -> list[Tile]:
    q = query.strip().lower()
    return [
        t
        for t in TILES
        if (not category or t.category == category)
        and (not q or q in t.name.lower() or q in t.blurb.lower() or q in t.category.lower())
    ]
