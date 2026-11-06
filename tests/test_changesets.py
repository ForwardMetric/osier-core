import uuid

import pytest
from conftest import AGENT, add, apply, fld, obj
from psycopg import sql

from osier import Principal
from osier.errors import (
    ApprovalError,
    InvalidNameError,
    NotFoundError,
    RecordValidationError,
    SchemaChangeError,
)


def test_crm_objects_installed_with_documentation(crm):
    schema = crm.describe_schema()
    names = {o["api_name"] for o in schema["objects"]}
    assert {"account", "contact", "lead", "opportunity", "activity"} <= names
    assert {"organization", "app_user", "role", "user_role"} <= names
    opp = next(o for o in schema["objects"] if o["api_name"] == "opportunity")
    assert opp["package"] == "crm" and opp["description"]
    stage = next(f for f in opp["fields"] if f["api_name"] == "stage")
    assert stage["default"] == "prospecting" and stage["track_history"] is True
    assert all(v["description"] for v in stage["values"])
    account = next(f for f in opp["fields"] if f["api_name"] == "account")
    assert account["target"] == "account" and account["on_delete"] == "cascade"
    contact = next(o for o in schema["objects"] if o["api_name"] == "contact")
    given = next(f for f in contact["fields"] if f["api_name"] == "given_name")
    assert given["semantic_ref"] == "https://schema.org/givenName"
    assert contact["semantic_ref"] == "https://schema.org/Person"


def test_create_custom_object_with_fields(crm):
    result = apply(
        crm,
        [
            obj(
                "c__loan",
                "Loan",
                [
                    fld("c__principal_amount", "decimal", required=True),
                    fld("c__reference", "text", unique=True),
                    fld("c__borrower", "lookup", options={"target": "account"}),
                ],
            )
        ],
    )
    loan = crm.snapshot().get_object("c__loan")
    assert set(loan.active_fields) == {"c__principal_amount", "c__reference", "c__borrower"}
    assert loan.fields["c__reference"].index_status == "active"
    assert loan.package is None and loan.owner.value == "custom"
    assert result.schema_version == crm.snapshot().version


def test_proposal_shows_ddl_summary_and_advice_without_changing_anything(crm):
    proposal = crm.schema.propose([add("account", "c__region", "text")])
    assert proposal.can_apply
    assert any('ADD COLUMN "c__region"' in ddl for ddl in proposal.ddl)
    assert proposal.summary == ["Add field 'c__region' (text) to 'account'."]
    assert any(
        i["severity"] == "advice" and "semantic_ref" in i["message"] for i in proposal.impact
    )
    assert "c__region" not in crm.snapshot().get_object("account").fields


# --- documentation and naming rules ---------------------------------------------------


@pytest.mark.parametrize(
    ("description", "code"),
    [
        (None, "DOCUMENTATION_REQUIRED"),
        ("", "DOCUMENTATION_REQUIRED"),
        ("Region", "DOCUMENTATION_TOO_THIN"),  # repeats the label
        ("TBD", "DOCUMENTATION_TOO_THIN"),
        ("Sales region.", "DOCUMENTATION_TOO_THIN"),  # too short
    ],
)
def test_fields_must_be_documented(crm, description, code):
    spec = fld("c__region", "text")
    spec["label"] = "Region"
    spec["description"] = description
    with pytest.raises(SchemaChangeError) as exc:
        crm.schema.propose([{"op": "add_field", "object": "account", "field": spec}])
    assert exc.value.code == code


def test_objects_must_be_documented(crm):
    op = obj("c__project", "Project")
    op["description"] = None
    with pytest.raises(SchemaChangeError) as exc:
        crm.schema.propose([op])
    assert exc.value.code == "DOCUMENTATION_REQUIRED"


@pytest.mark.parametrize("name", ["c__field1", "c__new_field", "c__data_2", "c__misc", "c__temp"])
def test_generic_field_names_are_rejected(crm, name):
    with pytest.raises(SchemaChangeError) as exc:
        crm.schema.propose([add("account", name, "text")])
    assert exc.value.code == "POOR_NAME"


def test_generic_object_names_are_rejected(crm):
    with pytest.raises(SchemaChangeError) as exc:
        crm.schema.propose([obj("c__table", "Table")])
    assert exc.value.code == "POOR_NAME"


def test_description_cannot_be_cleared_by_update(crm):
    with pytest.raises(SchemaChangeError) as exc:
        crm.schema.propose(
            [{"op": "update_field", "object": "account", "field": "website", "description": ""}]
        )
    assert exc.value.code == "DOCUMENTATION_REQUIRED"


def test_semantic_ref_must_be_a_uri(crm):
    with pytest.raises(SchemaChangeError) as exc:
        crm.schema.propose([add("account", "c__region", "text", semantic_ref="region")])
    assert exc.value.code == "INVALID_SEMANTIC_REF"
    apply(crm, [add("account", "c__region", "text", semantic_ref="https://schema.org/areaServed")])
    assert (
        crm.snapshot().get_object("account").fields["c__region"].semantic_ref.endswith("areaServed")
    )


# --- other rules --------------------------------------------------------------------------


def test_tenant_custom_fields_need_prefix_and_get_a_hint(crm):
    with pytest.raises(InvalidNameError) as exc:
        crm.schema.propose([add("account", "region", "text")])
    assert "c__region" in exc.value.hint


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
                "add_picklist_values": [{"value": "lending", "description": "Lenders"}],
            },
        ],
    )
    account = crm.snapshot().get_object("account")
    assert account.fields["name"].label == "Company"
    assert "lending" in [v["value"] for v in account.fields["industry"].options["values"]]


def test_tenants_cannot_create_protected_objects(crm):
    with pytest.raises(SchemaChangeError) as exc:
        crm.schema.propose([obj("c__secret_rule", "Secret Rule", protected=True)])
    assert exc.value.code == "NOT_ALLOWED"


def test_tenants_cannot_send_package_operations_directly(crm):
    with pytest.raises(SchemaChangeError) as exc:
        crm.schema.propose([{"op": "register_package", "package": "crm", "version": 1}])
    assert exc.value.code == "NOT_ALLOWED"


def test_malformed_change_set_is_rejected_with_details(crm):
    with pytest.raises(SchemaChangeError) as exc:
        crm.schema.propose([{"op": "drop_table", "object": "account"}])
    assert exc.value.code == "CHANGE_SET_MALFORMED"
    assert exc.value.details["errors"]


def test_unknown_type_lists_allowed_types(crm):
    with pytest.raises(SchemaChangeError) as exc:
        crm.schema.propose([add("account", "c__budget", "money")])
    assert "decimal" in exc.value.expected


def test_text_formats_and_numeric_bounds(crm):
    apply(
        crm,
        [
            add("account", "c__home_currency", "text", options={"format": "currency_code"}),
            add("account", "c__risk_score", "integer", options={"min": 1, "max": 10}),
        ],
    )
    with pytest.raises(Exception) as exc:
        crm.records.create("account", {"name": "A", "c__home_currency": "usd"})
    assert exc.value.code == "VALIDATION_FAILED"
    with pytest.raises(RecordValidationError):
        crm.records.create("account", {"name": "A", "c__risk_score": 11})
    rec = crm.records.create(
        "account", {"name": "A", "c__home_currency": "EUR", "c__risk_score": 3}
    )
    assert rec["c__home_currency"] == "EUR"
    with pytest.raises(SchemaChangeError):
        crm.schema.propose([add("account", "c__bad_format", "text", options={"format": "regex"})])


def test_unique_together(crm):
    apply(
        crm,
        [
            obj(
                "c__account_rating",
                "Account Rating",
                [
                    fld("c__rated_account", "lookup", options={"target": "account"}, required=True),
                    fld("c__rating_year", "integer", required=True),
                    fld("c__rating_grade", "text"),
                ],
                unique_together=[["c__rated_account", "c__rating_year"]],
            )
        ],
    )
    acme = crm.records.create("account", {"name": "Acme"})
    crm.records.create(
        "c__account_rating", {"c__rated_account": acme["id"], "c__rating_year": 2026}
    )
    crm.records.create(
        "c__account_rating", {"c__rated_account": acme["id"], "c__rating_year": 2027}
    )
    with pytest.raises(Exception) as exc:
        crm.records.create(
            "c__account_rating", {"c__rated_account": acme["id"], "c__rating_year": 2026}
        )
    assert exc.value.code == "UNIQUE_VIOLATION"
    assert exc.value.details["fields"] == ["c__rated_account", "c__rating_year"]


def test_required_field_on_populated_object_is_blocked_without_default(crm):
    account = crm.records.create("account", {"name": "Acme"})
    proposal = crm.schema.propose([add("account", "c__tier", "text", required=True)])
    assert not proposal.can_apply
    assert proposal.blockers[0]["records"] == 1
    with pytest.raises(SchemaChangeError) as exc:
        crm.schema.apply(proposal.id, diff_hash=proposal.diff_hash)
    assert exc.value.code == "CHANGE_SET_BLOCKED"
    apply(crm, [add("account", "c__tier", "text", required=True, default="standard")])
    assert crm.records.get("account", account["id"])["c__tier"] == "standard"


def test_unique_field_on_existing_object_builds_index_after_commit(crm):
    crm.records.create("account", {"name": "Acme"})
    result = apply(crm, [add("account", "c__customer_code", "text", unique=True)])
    assert result.post_apply[0]["status"] == "active"
    indexes = crm.conn.execute(
        "SELECT indexdef FROM pg_indexes WHERE schemaname = %s AND indexname LIKE 'ux_%%'",
        (crm.tenant.schema,),
    ).fetchall()
    assert any("c__customer_code" in row[0] and "UNIQUE" in row[0] for row in indexes)


def test_agent_cannot_approve_schema_changes(agent, admin):
    proposal = agent.schema.propose([add("account", "c__region", "text")])
    with pytest.raises(ApprovalError) as exc:
        agent.schema.apply(proposal.id, diff_hash=proposal.diff_hash)
    assert exc.value.code == "APPROVAL_REQUIRES_HUMAN"
    agent.as_principal(admin).schema.apply(proposal.id, diff_hash=proposal.diff_hash)
    assert "c__region" in agent.snapshot().get_object("account").active_fields


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
    apply(crm, [obj("c__project", "Project")])
    apply(crm, [add("account", "c__main_project", "lookup", options={"target": "c__project"})])
    proposal = crm.schema.propose([{"op": "retire_object", "object": "c__project"}])
    assert not proposal.can_apply
    assert proposal.blockers[0]["fields"] == ["account.c__main_project"]


def test_retired_names_cannot_be_reused(crm):
    apply(crm, [add("account", "c__legacy_code", "text")])
    apply(crm, [{"op": "retire_field", "object": "account", "field": "c__legacy_code"}])
    with pytest.raises(SchemaChangeError) as exc:
        crm.schema.propose([add("account", "c__legacy_code", "text")])
    assert exc.value.code == "FIELD_EXISTS"


def test_only_platform_can_propose_platform_changes(crm):
    op = obj("invoice", "Invoice")
    with pytest.raises(ApprovalError):
        crm.schema.propose([op], source="platform")
    platform = crm.as_principal(Principal.system())
    proposal = platform.schema.propose([op], source="platform")
    platform.schema.apply(proposal.id, diff_hash=proposal.diff_hash)
    assert crm.snapshot().get_object("invoice").owner.value == "standard"


def test_unknown_change_set(crm):
    with pytest.raises(NotFoundError) as exc:
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
    apply(crm, [add("account", "c__first_extra", "text")])
    apply(crm, [{"op": "retire_field", "object": "account", "field": "c__first_extra"}])
    with pytest.raises(SchemaChangeError) as exc:  # retired fields still count
        crm.schema.propose([add("account", "c__second_extra", "text")])
    assert exc.value.code == "FIELD_LIMIT_REACHED"
