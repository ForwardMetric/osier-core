"""Case management package: customer support cases and their comments.

schema.org has no type for a support case; cases are documented in their own terms.
Comments map to schema.org Comment.
"""

from __future__ import annotations

from ._dsl import create_object, field, lookup, picklist

NAME = "case_management"
VERSION = 1
LABEL = "Case Management"
DESCRIPTION = (
    "Customer support: cases (questions, problems, complaints, requests) raised by "
    "customers, their priority and status through to resolution, and the comments "
    "exchanged while working on them."
)
DEPENDS = ("crm",)

OPERATIONS = [
    create_object(
        "support_case",
        "Case",
        "Cases",
        "A customer issue to be resolved: a question, problem, incident, request, or "
        "complaint. Tracked from opening to closure with status, priority, and target dates.",
        fields=[
            field(
                "subject",
                "Subject",
                "text",
                "One-line summary of the customer's issue.",
                sdo="name",
                required=True,
            ),
            field(
                "description",
                "Description",
                "long_text",
                "Full description of the issue as reported, including steps and context.",
                sdo="description",
            ),
            field(
                "status",
                "Status",
                "picklist",
                "Where the case is in its lifecycle.",
                required=True,
                default="new",
                track_history=True,
                options=picklist(
                    ("new", "New", "Received, not yet reviewed."),
                    ("open", "Open", "Being worked on by the support team."),
                    (
                        "waiting_on_customer",
                        "Waiting on Customer",
                        "Needs information or action from the customer.",
                    ),
                    ("escalated", "Escalated", "Handed to a specialist or senior team."),
                    ("resolved", "Resolved", "A solution was provided; awaiting confirmation."),
                    ("closed", "Closed", "Finished; no further work expected."),
                ),
            ),
            field(
                "priority",
                "Priority",
                "picklist",
                "How urgently the case must be handled.",
                required=True,
                default="medium",
                track_history=True,
                options=picklist(
                    ("low", "Low", "Minor issue; no significant impact."),
                    ("medium", "Medium", "Normal impact; default priority."),
                    ("high", "High", "Significant impact on the customer's work."),
                    ("urgent", "Urgent", "Critical impact; needs immediate attention."),
                ),
            ),
            field(
                "case_type",
                "Type",
                "picklist",
                "The nature of the customer's request.",
                options=picklist(
                    ("question", "Question", "Asking how something works."),
                    ("problem", "Problem", "Something is not working as expected."),
                    ("incident", "Incident", "An outage or service interruption."),
                    ("request", "Request", "Asking for a change or new capability."),
                    ("complaint", "Complaint", "Expressing dissatisfaction."),
                ),
            ),
            field(
                "origin",
                "Origin",
                "picklist",
                "The channel through which the case arrived.",
                options=picklist(
                    ("email", "Email", "Sent by email."),
                    ("phone", "Phone", "Reported by phone."),
                    ("web", "Web", "Submitted through a web form or portal."),
                    ("chat", "Chat", "Raised in live chat or an AI assistant."),
                    ("social", "Social", "Raised on social media."),
                    ("in_person", "In Person", "Reported face to face."),
                ),
            ),
            field(
                "account",
                "Account",
                "lookup",
                "The customer organization the case is for.",
                options=lookup("account", "set_null"),
            ),
            field(
                "contact",
                "Contact",
                "lookup",
                "The person who raised the case.",
                options=lookup("contact", "set_null"),
            ),
            field(
                "parent_case",
                "Parent Case",
                "lookup",
                "A broader case this one is part of, e.g. one outage affecting many customers.",
                options=lookup("support_case", "set_null"),
            ),
            field(
                "is_escalated",
                "Is Escalated",
                "boolean",
                "True if the case has been escalated beyond first-line support.",
                required=True,
                default=False,
            ),
            field(
                "due_at",
                "Due At",
                "datetime",
                "Target time for resolving the case, e.g. from a service-level agreement.",
            ),
            field(
                "resolved_at",
                "Resolved At",
                "datetime",
                "When a solution was provided to the customer.",
            ),
            field("closed_at", "Closed At", "datetime", "When the case was closed."),
            field(
                "resolution",
                "Resolution",
                "long_text",
                "How the issue was resolved, written so it can be reused for similar cases.",
            ),
            field(
                "satisfaction_rating",
                "Satisfaction Rating",
                "integer",
                "Customer's satisfaction with the handling of the case, from 1 to 5.",
                options={"min": 1, "max": 5},
            ),
        ],
    ),
    create_object(
        "case_comment",
        "Case Comment",
        "Case Comments",
        "A message added to a case by the support team or the customer, either internal "
        "(visible only to your team) or public (visible to the customer).",
        sdo="Comment",
        fields=[
            field(
                "support_case",
                "Case",
                "lookup",
                "The case the comment belongs to. Retiring the case retires its comments.",
                required=True,
                options=lookup("support_case", "cascade"),
            ),
            field(
                "body", "Body", "long_text", "The text of the comment.", sdo="text", required=True
            ),
            field(
                "is_public",
                "Is Public",
                "boolean",
                "True if the customer can see the comment; false for internal notes.",
                required=True,
                default=False,
            ),
            field(
                "author_contact",
                "Author (Customer)",
                "lookup",
                "The customer contact who wrote the comment; empty if written by your team.",
                options=lookup("contact", "set_null"),
            ),
        ],
    ),
]

SEEDS: list[tuple[str, str, dict]] = []
