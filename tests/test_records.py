import threading
import uuid

import psycopg
import pytest
from conftest import ADMIN, AGENT, DATABASE_URL, HUMAN_FOR_AGENT, apply

from osier import Session
from osier.errors import (
    ConflictError,
    IntegrityError,
    NotFoundError,
    RecordValidationError,
)


@pytest.fixture
def acme(crm):
    return crm.records.create("account", {"name": "Acme", "industry": "technology"})


def test_create_and_get(crm, acme):
    assert acme["name"] == "Acme"
    assert acme["row_version"] == 1
    assert acme["created_by"] == str(ADMIN.user_id)
    assert acme["owner_id"] == str(ADMIN.user_id)
    assert crm.records.get("account", acme["id"])["industry"] == "technology"


def test_defaults_apply_when_omitted(crm, acme):
    opp = crm.records.create(
        "opportunity", {"name": "Deal", "account": acme["id"], "close_date": "2026-12-31"}
    )
    assert opp["stage"] == "prospecting"
    assert opp["close_date"] == "2026-12-31"


def test_validation_errors_are_structured(crm):
    with pytest.raises(RecordValidationError) as exc:
        crm.records.create(
            "account",
            {"industry": "farming", "annual_revenue": "abc", "nonsense": 1, "id": "x"},
        )
    errors = {e["field"]: e for e in exc.value.details["errors"]}
    assert errors["name"]["code"] == "REQUIRED_FIELD_MISSING"
    assert errors["industry"]["code"] == "INVALID_VALUE"
    assert "technology" in errors["industry"]["expected"]
    assert errors["annual_revenue"]["code"] == "INVALID_VALUE"
    assert errors["nonsense"]["code"] == "UNKNOWN_FIELD"
    assert errors["id"]["code"] == "READ_ONLY_FIELD"
    assert exc.value.retryable is False
    assert exc.value.to_dict()["code"] == "VALIDATION_FAILED"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", 123),  # strict: numbers are not silently turned into text
        ("name", "x" * 256),  # max_length
        ("website", "not a url"),
        ("annual_revenue", "1.234"),  # too many decimal places
    ],
)
def test_type_rules(crm, field, value):
    values = {"name": "Acme", field: value}
    with pytest.raises(RecordValidationError):
        crm.records.create("account", values)


def test_email_and_datetime_types(crm):
    apply(
        crm,
        [
            {
                "op": "add_field",
                "object": "contact",
                "field": {"api_name": "c_last_call", "label": "Last call", "type": "datetime"},
            }
        ],
    )
    with pytest.raises(RecordValidationError):
        crm.records.create("contact", {"last_name": "Doe", "email": "not-an-email"})
    with pytest.raises(RecordValidationError):  # time zone required
        crm.records.create("contact", {"last_name": "Doe", "c_last_call": "2026-01-01T09:00:00"})
    contact = crm.records.create(
        "contact",
        {"last_name": "Doe", "email": "jane@example.com", "c_last_call": "2026-01-01T09:00:00Z"},
    )
    assert contact["c_last_call"].startswith("2026-01-01T09:00:00")


def test_lookup_must_point_to_active_record(crm, acme):
    with pytest.raises(IntegrityError) as exc:
        crm.records.create(
            "opportunity", {"name": "D", "account": str(uuid.uuid4()), "close_date": "2026-01-01"}
        )
    assert exc.value.code == "LOOKUP_TARGET_NOT_FOUND"


def test_update_with_optimistic_concurrency(crm, acme):
    updated = crm.records.update(
        "account", acme["id"], {"website": "https://acme.test"}, expected_row_version=1
    )
    assert updated["row_version"] == 2
    with pytest.raises(ConflictError) as exc:
        crm.records.update("account", acme["id"], {"name": "Stale"}, expected_row_version=1)
    assert exc.value.retryable is True
    assert exc.value.details["current_row_version"] == 2


def test_update_without_real_change_does_not_bump_version(crm, acme):
    same = crm.records.update("account", acme["id"], {"name": "Acme"}, expected_row_version=1)
    assert same["row_version"] == 1


def test_required_field_cannot_be_cleared(crm, acme):
    with pytest.raises(RecordValidationError):
        crm.records.update("account", acme["id"], {"name": None}, expected_row_version=1)


def test_field_history_with_agent_attribution_and_provenance(agent):
    account = agent.records.create("account", {"name": "Acme"})
    opp = agent.records.create(
        "opportunity",
        {"name": "Deal", "account": account["id"], "close_date": "2026-06-30", "amount": "100.00"},
    )
    request_id = uuid.uuid4()
    agent.records.update(
        "opportunity",
        opp["id"],
        {"amount": "250.50", "stage": "proposal", "name": "Bigger deal"},
        expected_row_version=1,
        provenance={
            "source_type": "agent_extraction",
            "source_ref": "doc:42#p3",
            "confidence": 0.9,
        },
        request_id=request_id,
    )
    history = agent.records.history("opportunity", opp["id"])
    by_field = {h["field"]: h for h in history}
    assert set(by_field) == {"amount", "stage"}  # 'name' is not tracked
    assert by_field["amount"]["old_value"] == "100.00"
    assert by_field["amount"]["new_value"] == "250.50"
    assert by_field["stage"]["old_value"] == "prospecting"
    assert by_field["amount"]["actor_type"] == "agent"
    assert by_field["amount"]["agent_id"] == "test-agent"
    assert by_field["amount"]["on_behalf_of"] == str(HUMAN_FOR_AGENT)
    assert by_field["amount"]["request_id"] == str(request_id)
    assert by_field["amount"]["provenance"]["source_ref"] == "doc:42#p3"
    # Records created by an agent are owned by the person it acts for.
    assert opp["owner_id"] == str(HUMAN_FOR_AGENT)
    assert opp["created_by"] == str(AGENT.user_id)


def test_invalid_provenance_is_rejected(crm, acme):
    with pytest.raises(RecordValidationError) as exc:
        crm.records.update(
            "account",
            acme["id"],
            {"name": "X"},
            expected_row_version=1,
            provenance={"source_type": "rumour"},
        )
    assert exc.value.field == "provenance"


def test_unique_violation_is_reported_on_the_field(crm):
    apply(
        crm,
        [
            {
                "op": "add_field",
                "object": "account",
                "field": {"api_name": "c_code", "label": "Code", "type": "text", "unique": True},
            }
        ],
    )
    first = crm.records.create("account", {"name": "A", "c_code": "X1"})
    with pytest.raises(IntegrityError) as exc:
        crm.records.create("account", {"name": "B", "c_code": "X1"})
    assert exc.value.code == "UNIQUE_VIOLATION"
    assert exc.value.field == "c_code"
    assert exc.value.details["existing_record_id"] == first["id"]


def test_idempotency_key_returns_same_record(crm):
    first = crm.records.create("account", {"name": "Acme"}, idempotency_key="req-1")
    again = crm.records.create("account", {"name": "Acme"}, idempotency_key="req-1")
    assert again["id"] == first["id"]
    assert len(crm.records.list("account")) == 1
    with pytest.raises(IntegrityError) as exc:
        crm.records.create("account", {"name": "Other"}, idempotency_key="req-1")
    assert exc.value.code == "IDEMPOTENCY_KEY_REUSED"


def test_list_with_filters(crm):
    crm.records.create("account", {"name": "A", "industry": "retail"})
    crm.records.create("account", {"name": "B", "industry": "technology"})
    rows = crm.records.list("account", where={"industry": "retail"})
    assert [r["name"] for r in rows] == ["A"]
    with pytest.raises(RecordValidationError):
        crm.records.list("account", where={"nope": 1})


def test_retire_cascade_and_set_null(crm, acme):
    contact = crm.records.create("contact", {"last_name": "Doe", "account": acme["id"]})
    opp = crm.records.create(
        "opportunity", {"name": "Deal", "account": acme["id"], "close_date": "2026-01-01"}
    )
    report = crm.records.retire("account", acme["id"], expected_row_version=1)
    retired = {(r["object"], r["id"]) for r in report["retired"]}
    assert retired == {("account", acme["id"]), ("opportunity", opp["id"])}  # cascade
    assert report["cleared"] == [{"object": "contact", "field": "account", "count": 1}]
    assert crm.records.get("contact", contact["id"])["account"] is None  # set_null
    with pytest.raises(NotFoundError) as exc:
        crm.records.get("opportunity", opp["id"])
    assert exc.value.code == "RECORD_RETIRED"
    assert crm.records.get("opportunity", opp["id"], include_retired=True)["retired_at"]


def test_retire_restrict_blocks(crm, acme):
    apply(
        crm,
        [
            {
                "op": "create_object",
                "api_name": "c_loan",
                "label": "Loan",
                "fields": [
                    {
                        "api_name": "c_borrower",
                        "label": "Borrower",
                        "type": "lookup",
                        "options": {"target": "account", "on_delete": "restrict"},
                    }
                ],
            },
        ],
    )
    crm.records.create("c_loan", {"c_borrower": acme["id"]})
    with pytest.raises(IntegrityError) as exc:
        crm.records.retire("account", acme["id"], expected_row_version=1)
    assert exc.value.code == "RECORD_HAS_DEPENDENTS"
    # Nothing was changed: the whole retire rolled back.
    assert crm.records.get("account", acme["id"])["retired_at"] is None


def test_retired_record_cannot_be_updated(crm, acme):
    crm.records.retire("account", acme["id"], expected_row_version=1)
    with pytest.raises(IntegrityError):
        crm.records.update("account", acme["id"], {"name": "X"}, expected_row_version=2)


def test_retired_fields_are_hidden_and_rejected(crm, acme):
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
    crm.records.update("account", acme["id"], {"c_old": "v"}, expected_row_version=1)
    apply(crm, [{"op": "retire_field", "object": "account", "field": "c_old"}])
    assert "c_old" not in crm.records.get("account", acme["id"])
    with pytest.raises(RecordValidationError) as exc:
        crm.records.update("account", acme["id"], {"c_old": "w"}, expected_row_version=2)
    assert exc.value.details["errors"][0]["code"] == "FIELD_RETIRED"


def test_unknown_object_lists_available(crm):
    with pytest.raises(NotFoundError) as exc:
        crm.records.create("lead", {})
    assert exc.value.code == "OBJECT_NOT_FOUND"
    assert "account" in exc.value.expected


def test_new_field_is_usable_immediately_by_other_sessions(crm, tenant, acme):
    with psycopg.connect(DATABASE_URL, autocommit=True) as other_conn:
        other = Session(other_conn, tenant, ADMIN)
        assert "c_region" not in other.records.get("account", acme["id"])
        apply(
            crm,
            [
                {
                    "op": "add_field",
                    "object": "account",
                    "field": {"api_name": "c_region", "label": "Region", "type": "text"},
                }
            ],
        )
        updated = other.records.update(
            "account", acme["id"], {"c_region": "EMEA"}, expected_row_version=1
        )
        assert updated["c_region"] == "EMEA"


def test_concurrent_updates_one_wins(crm, tenant, acme):
    results: list[str] = []

    def worker(name: str) -> None:
        with psycopg.connect(DATABASE_URL, autocommit=True) as c:
            s = Session(c, tenant, ADMIN)
            try:
                s.records.update("account", acme["id"], {"name": name}, expected_row_version=1)
                results.append("ok")
            except ConflictError:
                results.append("conflict")

    threads = [threading.Thread(target=worker, args=(f"N{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["conflict", "conflict", "conflict", "ok"]
