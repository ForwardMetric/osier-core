"""Data steps (transform) and expression conditions, in CEL."""

import pytest

from osier.errors import OsierError

SCORE = {
    "name": "score-application",
    "description": "Compute a debt-to-income ratio and route on it.",
    "inputs": {
        "monthly_debt": {"type": "string", "description": "Monthly debt payments."},
        "monthly_income": {"type": "string", "description": "Monthly income."},
        "name": {"type": "string", "description": "Applicant name."},
    },
    "steps": [
        {
            "id": "calc",
            "type": "transform",
            "values": {
                "dti": "double(inputs.monthly_debt) / double(inputs.monthly_income)",
                "dti_percent": "math.round(values.dti * 100.0)",
                "first_name": "inputs.name.split(' ')[0]",
                "label": "upperAscii(inputs.name)",
            },
        },
        {
            "id": "risky",
            "type": "condition",
            "expression": "steps.calc.dti > 0.43",
            "then": "decline",
            "else": "accept",
        },
        {
            "id": "accept",
            "type": "finish",
            "output": {
                "decision": "accept",
                "dti": "{{steps.calc.dti_percent}}",
                "greeting": "Hi {{steps.calc.first_name}}",
            },
        },
        {
            "id": "decline",
            "type": "finish",
            "output": {"decision": "decline", "dti": "{{steps.calc.dti_percent}}"},
        },
    ],
}


def test_transform_computes_values_and_routes(crm):
    crm.workflows.save(SCORE)
    ok = crm.workflows.start(
        "score-application",
        {"monthly_debt": "1200.50", "monthly_income": "5000", "name": "Jane Doe"},
    )
    assert ok["status"] == "succeeded", ok["error"]
    assert ok["steps"]["calc"]["dti"] == pytest.approx(0.2401)
    assert ok["steps"]["calc"]["label"] == "JANE DOE"
    assert ok["output"] == {"decision": "accept", "dti": 24.0, "greeting": "Hi Jane"}
    bad = crm.workflows.start(
        "score-application", {"monthly_debt": "3000", "monthly_income": "5000", "name": "Al Roe"}
    )
    assert bad["output"] == {"decision": "decline", "dti": 60.0}


def test_runtime_errors_fail_the_step(crm):
    crm.workflows.save(SCORE)
    run = crm.workflows.start(
        "score-application", {"monthly_debt": "1", "monthly_income": "0", "name": "x"}
    )
    # double / 0.0 is infinity in CEL; non-finite numbers fail the step clearly
    assert run["status"] == "failed" and "finite" in run["error"]["message"]
    run = crm.workflows.start(
        "score-application", {"monthly_debt": "abc", "monthly_income": "1", "name": "x"}
    )
    assert run["status"] == "failed"
    assert run["error"]["code"] == "EXPRESSION_FAILED" and run["error"]["step"] == "calc"


def test_condition_expression_must_be_boolean(crm):
    crm.workflows.save(
        {
            **SCORE,
            "steps": [
                SCORE["steps"][0],
                {**SCORE["steps"][1], "expression": "steps.calc.dti"},
                *SCORE["steps"][2:],
            ],
        }
    )
    run = crm.workflows.start(
        "score-application", {"monthly_debt": "1", "monthly_income": "2", "name": "x"}
    )
    assert run["status"] == "failed" and "true or false" in run["error"]["message"]


@pytest.mark.parametrize(
    ("step", "message"),
    [
        ({"id": "t", "type": "transform", "values": {"a": "1 +"}}, "Syntax|parse"),
        ({"id": "t", "type": "transform", "values": {"a": "secrets.key"}}, "unknown names"),
        ({"id": "t", "type": "transform", "values": {"a": "steps.later.x"}}, "earlier step"),
        ({"id": "t", "type": "transform", "values": {"Bad": "1"}}, "lowercase"),
        ({"id": "t", "type": "transform", "values": {"a": "x" * 2001}}, "limited"),
        ({"id": "t", "type": "condition", "then": "end"}, "either 'when'"),
        (
            {
                "id": "t",
                "type": "condition",
                "expression": "true",
                "then": "end",
                "when": [{"value": 1}],
            },
            "either 'when'",
        ),
    ],
)
def test_invalid_expressions_are_refused_on_save(crm, step, message):
    with pytest.raises(OsierError, match=message):
        crm.workflows.preview(
            {"name": "x", "description": "x", "steps": [step, {"id": "later", "type": "finish"}]}
        )


def test_transform_with_records(crm):
    crm.workflows.save(
        {
            "name": "lead-summary",
            "description": "Summarize a lead's name.",
            "inputs": {"lead_id": {"type": "string", "description": "The lead."}},
            "steps": [
                {
                    "id": "lead",
                    "type": "get_record",
                    "object": "lead",
                    "record_id": "{{inputs.lead_id}}",
                },
                {
                    "id": "calc",
                    "type": "transform",
                    "values": {
                        "full": "steps.lead.record.given_name + ' ' "
                        "+ steps.lead.record.family_name",
                        "has_email": "has(steps.lead.record.email) "
                        "&& steps.lead.record.email != null",
                    },
                },
                {"id": "out", "type": "finish", "output": {"full": "{{steps.calc.full}}"}},
            ],
        }
    )
    lead = crm.records.create("lead", {"given_name": "Jane", "family_name": "Doe"})
    run = crm.workflows.start("lead-summary", {"lead_id": lead["id"]})
    assert run["status"] == "succeeded", run["error"]
    assert run["output"] == {"full": "Jane Doe"} and run["steps"]["calc"]["has_email"] is False
