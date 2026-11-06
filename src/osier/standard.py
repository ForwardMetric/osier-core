"""Standard objects shipped with the platform (package 'standard', version 1)."""

from __future__ import annotations

from typing import Any

from .changesets import ApplyResult
from .context import Principal
from .session import Session

PACKAGE = "standard"
VERSION = 1

OPERATIONS: list[dict[str, Any]] = [
    {
        "op": "create_object",
        "api_name": "account",
        "label": "Account",
        "plural_label": "Accounts",
        "description": "A company or organization you do business with.",
        "fields": [
            {"api_name": "name", "label": "Account Name", "type": "text", "required": True},
            {"api_name": "website", "label": "Website", "type": "url"},
            {
                "api_name": "industry",
                "label": "Industry",
                "type": "picklist",
                "options": {
                    "values": [
                        "financial_services",
                        "healthcare",
                        "manufacturing",
                        "retail",
                        "technology",
                        "other",
                    ]
                },
            },
            {
                "api_name": "annual_revenue",
                "label": "Annual Revenue",
                "type": "decimal",
                "options": {"precision": 18, "scale": 2},
            },
            {"api_name": "description", "label": "Description", "type": "long_text"},
        ],
    },
    {
        "op": "create_object",
        "api_name": "contact",
        "label": "Contact",
        "plural_label": "Contacts",
        "description": "A person, usually associated with an account.",
        "fields": [
            {"api_name": "first_name", "label": "First Name", "type": "text"},
            {"api_name": "last_name", "label": "Last Name", "type": "text", "required": True},
            {"api_name": "email", "label": "Email", "type": "email", "indexed": True},
            {"api_name": "phone", "label": "Phone", "type": "text", "options": {"max_length": 40}},
            {
                "api_name": "account",
                "label": "Account",
                "type": "lookup",
                "options": {"target": "account", "on_delete": "set_null"},
            },
        ],
    },
    {
        "op": "create_object",
        "api_name": "opportunity",
        "label": "Opportunity",
        "plural_label": "Opportunities",
        "description": "A potential deal with an account.",
        "fields": [
            {"api_name": "name", "label": "Opportunity Name", "type": "text", "required": True},
            {
                "api_name": "account",
                "label": "Account",
                "type": "lookup",
                "required": True,
                "options": {"target": "account", "on_delete": "cascade"},
            },
            {
                "api_name": "amount",
                "label": "Amount",
                "type": "decimal",
                "track_history": True,
                "options": {"precision": 18, "scale": 2},
            },
            {
                "api_name": "stage",
                "label": "Stage",
                "type": "picklist",
                "required": True,
                "track_history": True,
                "default": "prospecting",
                "options": {
                    "values": [
                        "prospecting",
                        "qualification",
                        "proposal",
                        "negotiation",
                        "closed_won",
                        "closed_lost",
                    ]
                },
            },
            {
                "api_name": "close_date",
                "label": "Close Date",
                "type": "date",
                "required": True,
                "track_history": True,
            },
        ],
    },
]


def install(session: Session) -> ApplyResult:
    """Install the standard objects into a freshly provisioned tenant."""
    platform = session.as_principal(Principal.system(source="platform"))
    proposal = platform.schema.propose(OPERATIONS, source="platform")
    return platform.schema.apply(proposal.id, diff_hash=proposal.diff_hash)
