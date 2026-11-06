"""Core package: your organization, its users, and their roles. Installed in every tenant."""

from __future__ import annotations

from ._dsl import create_object, field, lookup, picklist, postal_address

NAME = "core"
VERSION = 1
LABEL = "Core"
DESCRIPTION = (
    "Your own organization, the people who use the platform, and the roles that "
    "determine what they may do. Installed in every tenant and cannot be removed."
)
DEPENDS: tuple[str, ...] = ()

OPERATIONS = [
    create_object(
        "organization",
        "Organization",
        "Organizations",
        "Your own organization and its internal units (company, division, department, team). "
        "Users belong to an organization. Not to be confused with Account, which represents "
        "organizations you do business with.",
        sdo="Organization",
        fields=[
            field(
                "name",
                "Name",
                "text",
                "The commonly used name of the organization or unit.",
                sdo="name",
                required=True,
            ),
            field(
                "legal_name",
                "Legal Name",
                "text",
                "The registered legal name, if different from the common name.",
                sdo="legalName",
            ),
            field(
                "unit_type",
                "Unit Type",
                "picklist",
                "What kind of organizational unit this is; the top-level record is the company.",
                required=True,
                default="company",
                options=picklist(
                    ("company", "Company", "The top-level legal entity (your tenant)."),
                    ("division", "Division", "A major business unit within the company."),
                    ("department", "Department", "A functional group, e.g. Finance or Sales."),
                    ("team", "Team", "A small working group within a department."),
                ),
            ),
            field(
                "parent_organization",
                "Parent Organization",
                "lookup",
                "The larger unit this one belongs to; empty for the top-level company.",
                sdo="parentOrganization",
                options=lookup("organization"),
            ),
            field(
                "email",
                "Email",
                "email",
                "General contact email address of the organization.",
                sdo="email",
            ),
            field(
                "telephone",
                "Telephone",
                "text",
                "Main telephone number of the organization.",
                sdo="telephone",
                options={"format": "phone", "max_length": 40},
            ),
            field("url", "Website", "url", "Public website of the organization.", sdo="url"),
            field(
                "tax_id",
                "Tax ID",
                "text",
                "Tax identification number (e.g. EIN in the US, VAT number in the EU).",
                sdo="taxID",
                options={"max_length": 50},
            ),
            *postal_address("", "Address", "organization's main address"),
        ],
    ),
    create_object(
        "app_user",
        "User",
        "Users",
        "A person who can sign in and use the platform, directly or through an AI agent. "
        "Each user belongs to an organization and gets permissions through role assignments.",
        sdo="Person",
        protected=True,
        unique_together=[["identity_provider", "identity_subject"]],
        fields=[
            field(
                "display_name",
                "Display Name",
                "text",
                "Full name as shown to other users, e.g. 'Ada Lovelace'.",
                sdo="name",
                required=True,
            ),
            field(
                "given_name",
                "Given Name",
                "text",
                "First or given name of the user.",
                sdo="givenName",
            ),
            field(
                "family_name",
                "Family Name",
                "text",
                "Last or family name of the user.",
                sdo="familyName",
            ),
            field(
                "email",
                "Email",
                "email",
                "Email address used to sign in and receive notifications; unique per tenant.",
                sdo="email",
                required=True,
                unique=True,
            ),
            field(
                "status",
                "Status",
                "picklist",
                "Whether the user can currently sign in.",
                required=True,
                default="invited",
                track_history=True,
                options=picklist(
                    ("invited", "Invited", "Invited but has not signed in yet."),
                    ("active", "Active", "Can sign in and use the platform."),
                    ("suspended", "Suspended", "Temporarily blocked from signing in."),
                    ("deactivated", "Deactivated", "Permanently removed from access."),
                ),
            ),
            field(
                "organization",
                "Organization",
                "lookup",
                "The organization or unit the user belongs to.",
                sdo="worksFor",
                options=lookup("organization", "restrict"),
            ),
            field(
                "job_title",
                "Job Title",
                "text",
                "The user's job title, e.g. 'Loan Officer'.",
                sdo="jobTitle",
            ),
            field(
                "telephone",
                "Telephone",
                "text",
                "Work telephone number of the user.",
                sdo="telephone",
                options={"format": "phone", "max_length": 40},
            ),
            field(
                "time_zone",
                "Time Zone",
                "text",
                "The user's IANA time zone, used to display dates and schedule work.",
                options={"format": "time_zone", "max_length": 64},
            ),
            field(
                "locale",
                "Locale",
                "text",
                "Preferred language and region as a BCP 47 tag, e.g. en-US.",
                sdo="knowsLanguage",
                options={"format": "language_tag", "max_length": 35},
            ),
            field(
                "identity_provider",
                "Identity Provider",
                "text",
                "Issuer URL of the identity provider the user signs in with (OIDC 'iss').",
                options={"max_length": 500},
            ),
            field(
                "identity_subject",
                "Identity Subject",
                "text",
                "The user's stable identifier at the identity provider (OIDC 'sub').",
                options={"max_length": 255},
            ),
            field(
                "last_sign_in_at",
                "Last Sign-in",
                "datetime",
                "When the user last signed in successfully.",
            ),
        ],
    ),
    create_object(
        "role",
        "Role",
        "Roles",
        "A named set of responsibilities, such as Administrator or Loan Officer. Permissions "
        "are granted to roles, and users receive them through role assignments.",
        sdo="Role",
        protected=True,
        fields=[
            field(
                "name",
                "Name",
                "text",
                "Human-readable role name, e.g. 'Loan Officer'.",
                sdo="roleName",
                required=True,
            ),
            field(
                "key",
                "Key",
                "text",
                "Stable machine identifier for the role, e.g. 'loan_officer'. Never reused.",
                required=True,
                unique=True,
                options={"max_length": 64},
            ),
            field(
                "description",
                "Description",
                "long_text",
                "What people in this role do and what they should be allowed to access.",
                sdo="description",
            ),
            field(
                "is_system_role",
                "Is System Role",
                "boolean",
                "True for roles provided by the platform, which cannot be deleted.",
                required=True,
                default=False,
            ),
        ],
    ),
    create_object(
        "user_role",
        "Role Assignment",
        "Role Assignments",
        "Gives a user a role, optionally for a limited period. A user can hold several roles; "
        "each user-role pair appears at most once.",
        sdo="OrganizationRole",
        protected=True,
        unique_together=[["app_user", "role"]],
        fields=[
            field(
                "app_user",
                "User",
                "lookup",
                "The user who receives the role.",
                required=True,
                options=lookup("app_user", "cascade"),
            ),
            field(
                "role",
                "Role",
                "lookup",
                "The role being assigned to the user.",
                required=True,
                options=lookup("role", "restrict"),
            ),
            field(
                "start_date",
                "Start Date",
                "date",
                "First day the assignment is effective; empty means immediately.",
                sdo="startDate",
            ),
            field(
                "end_date",
                "End Date",
                "date",
                "Last day the assignment is effective; empty means no end date.",
                sdo="endDate",
            ),
        ],
    ),
]

SEEDS = [
    (
        "role",
        "role:admin",
        {
            "name": "Administrator",
            "key": "admin",
            "is_system_role": True,
            "description": "Full access: can change objects, fields, permissions, and all data.",
        },
    ),
    (
        "role",
        "role:member",
        {
            "name": "Member",
            "key": "member",
            "is_system_role": True,
            "description": "Standard access for everyday work; no schema or permission changes.",
        },
    ),
]
