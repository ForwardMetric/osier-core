"""Integrations package (system): the tenant's connections to external systems."""

from __future__ import annotations

from ._dsl import create_object, field, lookup, picklist

NAME = "integrations"
VERSION = 1
LABEL = "Integrations"
DESCRIPTION = (
    "Connections to external systems (email providers, marketing platforms, other "
    "apps) that agents use through connectors. Managed by the platform; installed in "
    "every tenant."
)
DEPENDS = ("core",)

OPERATIONS = [
    create_object(
        "connection",
        "Connection",
        "Connections",
        "A tenant's configured link to an external system through a connector, such as "
        "a company SMTP account or a user's mailbox. Credentials are stored separately, "
        "encrypted, and are never visible here.",
        access="engine_managed",
        managed_by="connectors",
        unique_together=[["name", "owner_user"]],
        fields=[
            field(
                "name",
                "Name",
                "text",
                "Short name people and agents use to pick this connection, e.g. 'Sales SMTP'.",
                required=True,
            ),
            field(
                "connector_type",
                "Connector Type",
                "text",
                "Key of the connector type this connection uses, e.g. 'smtp' or 'dev_outbox'.",
                required=True,
                options={"max_length": 64},
            ),
            field(
                "purpose",
                "Purpose",
                "picklist",
                "What the connection is used for; agents and routing select connections by "
                "capability and purpose.",
                required=True,
                default="transactional",
                options=picklist(
                    (
                        "transactional",
                        "Transactional",
                        "One-to-one messages tied to a record, e.g. follow-ups and case replies.",
                    ),
                    (
                        "marketing",
                        "Marketing",
                        "Messages to lists or audiences; consent and unsubscribe rules apply.",
                    ),
                    ("general", "General", "Any other use, for non-email connectors."),
                ),
            ),
            field(
                "scope",
                "Scope",
                "picklist",
                "Whether the connection is shared by the organization or belongs to one user.",
                required=True,
                default="organization",
                options=picklist(
                    ("organization", "Organization", "Shared company account."),
                    ("user", "User", "A single user's own account, e.g. their mailbox."),
                ),
            ),
            field(
                "owner_user",
                "Owner",
                "lookup",
                "The user a user-scoped connection belongs to; empty for organization scope.",
                options=lookup("app_user", "cascade"),
            ),
            field(
                "is_default",
                "Is Default",
                "boolean",
                "True if this is the default connection for its capability and purpose.",
                required=True,
                default=False,
            ),
            field(
                "status",
                "Status",
                "picklist",
                "Whether the connection currently works.",
                required=True,
                default="pending",
                track_history=True,
                options=picklist(
                    ("pending", "Pending", "Created but not verified yet."),
                    ("active", "Active", "Verified and usable."),
                    ("error", "Error", "The last attempt to use it failed."),
                    ("revoked", "Revoked", "Disconnected; credentials removed."),
                ),
            ),
            field(
                "last_error",
                "Last Error",
                "long_text",
                "Message from the most recent failure, to help fix the connection.",
            ),
            field(
                "connected_at",
                "Connected At",
                "datetime",
                "When the connection was set up and verified.",
            ),
            field(
                "last_used_at",
                "Last Used At",
                "datetime",
                "When an action last ran through this connection.",
            ),
        ],
    ),
]

SEEDS: list[tuple[str, str, dict]] = []
