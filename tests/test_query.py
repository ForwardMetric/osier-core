import pytest

from osier.errors import RecordValidationError


@pytest.fixture
def data(crm):
    acme = crm.records.create(
        "account", {"name": "Acme Corp", "industry": "technology", "number_of_employees": 500}
    )
    beta = crm.records.create(
        "account",
        {"name": "Beta_Bank", "industry": "financial_services", "number_of_employees": 50},
    )
    crm.records.create("account", {"name": "Gamma 100%", "number_of_employees": 5})
    for i, status in enumerate(["new", "working", "qualified"]):
        crm.records.create(
            "lead",
            {
                "family_name": f"Lead{i}",
                "company_name": "Acme Corp",
                "status": status,
                "converted_account": acme["id"],
            },
        )
    return {"acme": acme, "beta": beta}


def names(result):
    return [r["name"] for r in result["records"]]


def q(crm, **kw):
    return crm.records.query({"object": "account", **kw})


def test_comparison_and_sorting(crm, data):
    result = q(
        crm,
        filters=[{"field": "number_of_employees", "op": "gte", "value": 50}],
        order_by=[{"field": "number_of_employees", "direction": "desc"}],
    )
    assert names(result) == ["Acme Corp", "Beta_Bank"]


def test_in_and_not_in(crm, data):
    assert names(
        q(crm, filters=[{"field": "industry", "op": "in", "value": ["technology", "retail"]}])
    ) == ["Acme Corp"]
    assert "Acme Corp" not in names(
        q(crm, filters=[{"field": "industry", "op": "not_in", "value": ["technology"]}])
    )


def test_contains_is_case_insensitive_and_escapes_wildcards(crm, data):
    assert names(q(crm, filters=[{"field": "name", "op": "contains", "value": "acme"}])) == [
        "Acme Corp"
    ]
    # '_' and '%' are literal characters, not wildcards
    assert names(q(crm, filters=[{"field": "name", "op": "contains", "value": "a_b"}])) == [
        "Beta_Bank"
    ]
    assert names(q(crm, filters=[{"field": "name", "op": "contains", "value": "100%"}])) == [
        "Gamma 100%"
    ]
    assert names(q(crm, filters=[{"field": "name", "op": "starts_with", "value": "be"}])) == [
        "Beta_Bank"
    ]


def test_is_null(crm, data):
    assert names(q(crm, filters=[{"field": "industry", "op": "is_null", "value": True}])) == [
        "Gamma 100%"
    ]


def test_paging(crm, data):
    first = q(crm, limit=2, order_by=[{"field": "name"}])
    assert first["count"] == 2 and first["has_more"] is True
    second = q(crm, limit=2, offset=2, order_by=[{"field": "name"}])
    assert second["count"] == 1 and second["has_more"] is False


def test_expand_lookup(crm, data):
    result = crm.records.query({"object": "lead", "expand": ["converted_account"], "limit": 1})
    assert result["records"][0]["expanded"]["converted_account"]["name"] == "Acme Corp"


def test_system_fields_are_filterable(crm, data):
    result = q(crm, filters=[{"field": "id", "op": "eq", "value": data["beta"]["id"]}])
    assert names(result) == ["Beta_Bank"]


@pytest.mark.parametrize(
    ("query", "code"),
    [
        ({"filters": [{"field": "nope", "value": 1}]}, "UNKNOWN_FIELD"),
        ({"filters": [{"field": "number_of_employees", "value": "many"}]}, "INVALID_VALUE"),
        (
            {"filters": [{"field": "number_of_employees", "op": "contains", "value": "5"}]},
            "INVALID_OPERATOR",
        ),
        ({"filters": [{"field": "name", "op": "like", "value": "x"}]}, "QUERY_MALFORMED"),
        ({"filters": [{"field": "name", "value": None}]}, "INVALID_VALUE"),
        ({"order_by": [{"field": "description"}]}, "INVALID_OPERATOR"),
        ({"expand": ["name"]}, "INVALID_EXPAND"),
        ({"limit": 1000}, "QUERY_MALFORMED"),
        ({"sql": "select 1"}, "QUERY_MALFORMED"),
    ],
)
def test_invalid_queries_have_structured_errors(crm, data, query, code):
    with pytest.raises(RecordValidationError) as exc:
        q(crm, **query)
    assert exc.value.code == code


def test_retired_records_are_excluded_unless_asked(crm, data):
    crm.records.retire("account", data["beta"]["id"], expected_row_version=1)
    assert "Beta_Bank" not in names(q(crm))
    assert "Beta_Bank" in names(q(crm, include_retired=True))
