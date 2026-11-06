import uuid

import pytest
from conftest import ADMIN, AGENT, apply
from psycopg import sql

from osier import Principal
from osier.errors import ApprovalError, InvalidNameError, SchemaChangeError


def test_standard_objects_installed(crm):
    schema = crm.describe_schema()
    names = {o["api_name"] for o in schema["objects"]}
    assert names == {"account", "contact", "opportunity"}
    assert schema["schema_version"] == 1
    opp = next(o for o in schema["objects"] if o["api_name"] == "opportunity")
    stage = next(f for f in opp["fields"] if f["api_name"] == "stage")
    assert stage["default"] == "prospecting"
    assert stage["track_history"] is True
    account = next(f for f in opp["fields"] if f["api_name"] == "account")
    assert account["target"] == "account" and account["on_delete"] == "cascade"


def test_create_custom_object_with_fields(crm):
    result = apply(
        crm,
        [
            {
                "op": "create_object",
                "api_name": "c_loan",
                "label": "Loan",
                "fields": [
                    {
                        "api_name": "c_amount",
                        "label": "Amount",
                        "type": "decimal",
                        "required": True,
                    },
                    {"api_name": "c_reference", "label": "Ref", "type": "text", "unique": True},
                    {
                        "api_name": "c_account",
                        "label": "Account",
                        "type": "lookup",
                        "options": {"target": "account"},
                    },
                ],
            }
        ],
    )
    assert result.schema_version == 2
    loan = crm.snapshot().get_object("c_loan")
    assert set(loan.active_fields) == {"c_amount", "c_reference", "c_account"}
    assert loan.fields["c_reference"].index_status == "active"


def test_proposal_shows_ddl_and_summary_without_changing_anything(crm):
    proposal = crm.schema.propose(
        [
            {
                "op": "add_field",
                "object": "account",
                "field": {"api_name": "c_region", "label": "Region", "type": "text"},
            }
        ]
    )
    assert proposal.can_apply
    assert any('ADD COLUMN "c_region"' in ddl for ddl in proposal.ddl)
    assert proposal.summary == ["Add field 'c_region' (text) to 'account'."]
    assert "c_region" not in crm.snapshot().get_object("account").fields


def test_tenant_custom_fields_need_prefix_and_get_a_hint(crm):
    with pytest.raises(InvalidNameError) as exc:
        crm.schema.propose(
            [
                {
                    "op": "add_field",
                    "object": "account",
                    "field": {"api_name": "region", "label": "Region", "type": "text"},
                }
            ]
        )
    assert "c_region" in exc.value.hint


def test_tenant_cannot_retire_standard_field(crm):
    with pytest.raises(SchemaChangeError) as exc:
        crm.schema.propose([{"op": "retire_field", "object": "account", "field": "website"}])
    assert exc.value.code == "STANDARD_ITEM_PROTECTED"


def test_tenant_can_relabel_standard_field_and_add_picklist_value(crm):
    apply(
        crm,
        [
            {"op": "update_field", "object": "account", "field": "name", "label": "Company"},
            {
                "op": "update_field",
                "object": "account",
                "field": "industry",
                "add_picklist_values": ["lending"],
            },
        ],
    )
    account = crm.snapshot().get_object("account")
    assert account.fields["name"].label == "Company"
    assert "lending" in [v["value"] for v in account.fields["industry"].options["values"]]


def test_malformed_change_set_is_rejected_with_details(crm):
    with pytest.raises(SchemaChangeError) as exc:
        crm.schema.propose([{"op": "drop_table", "object": "account"}])
    assert exc.value.code == "CHANGE_SET_MALFORMED"
    assert exc.value.details["errors"]


def test_unknown_type_lists_allowed_types(crm):
    with pytest.raises(SchemaChangeError) as exc:
        crm.schema.propose(
            [
                {
                    "op": "add_field",
                    "object": "account",
                    "field": {"api_name": "c_x", "label": "X", "type": "money"},
                }
            ]
        )
    assert "decimal" in exc.value.expected


def test_required_field_on_populated_object_is_blocked_without_default(crm):
    account = crm.records.create("account", {"name": "Acme"})
    assert account["id"]
    proposal = crm.schema.propose(
        [
            {
                "op": "add_field",
                "object": "account",
                "field": {"api_name": "c_tier", "label": "Tier", "type": "text", "required": True},
            }
        ]
    )
    assert not proposal.can_apply
    assert proposal.blockers[0]["records"] == 1
    with pytest.raises(SchemaChangeError) as exc:
        crm.schema.apply(proposal.id, diff_hash=proposal.diff_hash)
    assert exc.value.code == "CHANGE_SET_BLOCKED"

    # With a default it is fine, and existing records get the default.
    apply(
        crm,
        [
            {
                "op": "add_field",
                "object": "account",
                "field": {
                    "api_name": "c_tier",
                    "label": "Tier",
                    "type": "text",
                    "required": True,
                    "default": "standard",
                },
            }
        ],
    )
    assert crm.records.get("account", account["id"])["c_tier"] == "standard"


def test_unique_field_on_existing_object_builds_index_after_commit(crm):
    crm.records.create("account", {"name": "Acme"})
    result = apply(
        crm,
        [
            {
                "op": "add_field",
                "object": "account",
                "field": {"api_name": "c_code", "label": "Code", "type": "text", "unique": True},
            }
        ],
    )
    assert result.post_apply[0]["status"] == "active"
    indexes = crm.conn.execute(
        "SELECT indexdef FROM pg_indexes WHERE schemaname = %s AND indexname LIKE 'ux_%%'",
        (crm.tenant.schema,),
    ).fetchall()
    assert any("c_code" in row[0] and "UNIQUE" in row[0] for row in indexes)


def test_agent_cannot_approve_schema_changes(agent):
    proposal = agent.schema.propose(
        [
            {
                "op": "add_field",
                "object": "account",
                "field": {"api_name": "c_region", "label": "Region", "type": "text"},
            }
        ]
    )
    with pytest.raises(ApprovalError) as exc:
        agent.schema.apply(proposal.id, diff_hash=proposal.diff_hash)
    assert exc.value.code == "APPROVAL_REQUIRES_HUMAN"
    # A human approves the agent's proposal.
    agent.as_principal(ADMIN).schema.apply(proposal.id, diff_hash=proposal.diff_hash)
    assert "c_region" in agent.snapshot().get_object("account").active_fields


def test_diff_hash_must_match(crm):
    proposal = crm.schema.propose(
        [{"op": "update_object", "object": "account", "label": "Customer"}]
    )
    with pytest.raises(ApprovalError) as exc:
        crm.schema.apply(proposal.id, diff_hash="0" * 64)
    assert exc.value.code == "DIFF_HASH_MISMATCH"


def test_stale_proposal_is_superseded(crm):
    first = crm.schema.propose([{"op": "update_object", "object": "account", "label": "A"}])
    second = crm.schema.propose([{"op": "update_object", "object": "contact", "label": "C"}])
    crm.schema.apply(first.id, diff_hash=first.diff_hash)
    with pytest.raises(SchemaChangeError) as exc:
        crm.schema.apply(second.id, diff_hash=second.diff_hash)
    assert exc.value.code == "CHANGE_SET_STALE"
    status = crm.conn.execute(
        sql.SQL("SELECT status FROM {} WHERE id = %s").format(
            sql.Identifier(crm.tenant.schema, "meta_change_sets")
        ),
        (second.id,),
    ).fetchone()[0]
    assert status == "superseded"


def test_change_set_cannot_be_applied_twice(crm):
    proposal = crm.schema.propose([{"op": "update_object", "object": "account", "label": "A"}])
    crm.schema.apply(proposal.id, diff_hash=proposal.diff_hash)
    with pytest.raises(SchemaChangeError) as exc:
        crm.schema.apply(proposal.id, diff_hash=proposal.diff_hash)
    assert exc.value.code == "CHANGE_SET_NOT_PENDING"


def test_retiring_object_referenced_by_lookup_is_blocked(crm):
    apply(crm, [{"op": "create_object", "api_name": "c_project", "label": "Project"}])
    apply(
        crm,
        [
            {
                "op": "add_field",
                "object": "account",
                "field": {
                    "api_name": "c_project",
                    "label": "Project",
                    "type": "lookup",
                    "options": {"target": "c_project"},
                },
            }
        ],
    )
    proposal = crm.schema.propose([{"op": "retire_object", "object": "c_project"}])
    assert not proposal.can_apply
    assert proposal.blockers[0]["fields"] == ["account.c_project"]


def test_retired_names_cannot_be_reused(crm):
    apply(
        crm,
        [
            {
                "op": "add_field",
                "object": "account",
                "field": {"api_name": "c_old", "label": "Old", "type": "text"},
            }
        ],
    )
    apply(crm, [{"op": "retire_field", "object": "account", "field": "c_old"}])
    with pytest.raises(SchemaChangeError) as exc:
        crm.schema.propose(
            [
                {
                    "op": "add_field",
                    "object": "account",
                    "field": {"api_name": "c_old", "label": "Old", "type": "text"},
                }
            ]
        )
    assert exc.value.code == "FIELD_EXISTS"


def test_only_platform_can_propose_platform_changes(crm):
    with pytest.raises(ApprovalError):
        crm.schema.propose(
            [{"op": "create_object", "api_name": "invoice", "label": "Invoice"}], source="platform"
        )
    platform = crm.as_principal(Principal.system())
    proposal = platform.schema.propose(
        [{"op": "create_object", "api_name": "invoice", "label": "Invoice"}], source="platform"
    )
    platform.schema.apply(proposal.id, diff_hash=proposal.diff_hash)
    assert crm.snapshot().get_object("invoice").owner.value == "standard"


def test_unknown_change_set(crm):
    with pytest.raises(Exception) as exc:
        crm.schema.apply(uuid.uuid4(), diff_hash="x")
    assert exc.value.code == "CHANGE_SET_NOT_FOUND"


def test_agent_proposals_are_attributed(agent):
    proposal = agent.schema.propose(
        [{"op": "update_object", "object": "account", "label": "Customer"}]
    )
    row = agent.conn.execute(
        sql.SQL(
            "SELECT proposed_by, proposed_by_type, proposed_by_agent FROM {} WHERE id = %s"
        ).format(sql.Identifier(agent.tenant.schema, "meta_change_sets")),
        (proposal.id,),
    ).fetchone()
    assert row == (AGENT.user_id, "agent", "test-agent")


def test_field_limit(crm, monkeypatch):
    from osier import changesets

    current = len(crm.snapshot().get_object("account").fields)
    monkeypatch.setattr(changesets, "MAX_FIELDS_PER_OBJECT", current + 1)
    apply(
        crm,
        [
            {
                "op": "add_field",
                "object": "account",
                "field": {"api_name": "c_one", "label": "One", "type": "text"},
            }
        ],
    )
    apply(crm, [{"op": "retire_field", "object": "account", "field": "c_one"}])
    with pytest.raises(SchemaChangeError) as exc:  # retired fields still count
        crm.schema.propose(
            [
                {
                    "op": "add_field",
                    "object": "account",
                    "field": {"api_name": "c_two", "label": "Two", "type": "text"},
                }
            ]
        )
    assert exc.value.code == "FIELD_LIMIT_REACHED"
