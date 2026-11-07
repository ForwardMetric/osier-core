"""AI steps in workflows, with a scripted fake Claude client (no network, no spend)."""

import asyncio
import json
from types import SimpleNamespace

import psycopg
import pytest
from conftest import DATABASE_URL, agent_for

from osier import ai
from osier.ai import AIService
from osier.connectors.vault import CredentialBroker
from osier.errors import OsierError
from osier.jobs import Worker, default_backend

MODEL = "test-model"


def text(t):
    return SimpleNamespace(type="text", text=t)


def tool(name, args, id_=None):
    return SimpleNamespace(type="tool_use", name=name, input=args, id=id_ or f"call_{name}")


def reply(*blocks, stop="tool_use", usage=(100, 20)):
    return SimpleNamespace(
        content=list(blocks),
        stop_reason=stop,
        model=MODEL,
        stop_details=None,
        usage=SimpleNamespace(
            input_tokens=usage[0], output_tokens=usage[1], cache_read_input_tokens=0
        ),
    )


class FakeClient:
    def __init__(self, script):
        self.script = list(script)
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **request):
        self.requests.append({**request, "messages": list(request["messages"])})
        step = self.script.pop(0)
        return step(request) if callable(step) else step


@pytest.fixture
def broker():
    return CredentialBroker(CredentialBroker.generate_key())


@pytest.fixture
def fake(broker):
    holder = {"client": None, "keys": []}

    def factory(api_key):
        holder["keys"].append(api_key)
        return holder["client"]

    ai.configure_runtime(broker=broker, client_factory=factory)
    yield holder
    ai.configure_runtime(broker=None, client_factory=ai._default_client_factory)


@pytest.fixture
def ai_ready(crm, broker, fake):
    AIService(crm, broker).update({"model": MODEL}, api_key="sk-test-secret")
    return crm


ASSESS = {
    "name": "assess-lead",
    "description": "Ask AI to assess a lead, then ask a person before marking it.",
    "inputs": {"lead_id": {"type": "string", "description": "The lead."}},
    "steps": [
        {"id": "lead", "type": "get_record", "object": "lead", "record_id": "{{inputs.lead_id}}"},
        {
            "id": "assess",
            "type": "agent",
            "instructions": "Decide whether this lead is worth pursuing.",
            "data": {"lead": "{{steps.lead.record}}"},
            "tools": ["query_records"],
            "objects": ["lead"],
            "output": {
                "decision": {
                    "type": "string",
                    "enum": ["pursue", "drop"],
                    "description": "The decision.",
                },
                "reason": {"type": "string", "description": "Why, in one sentence."},
            },
        },
        {
            "id": "approve",
            "type": "approval",
            "message": "AI says {{steps.assess.output.decision}}: {{steps.assess.output.reason}}",
        },
        {
            "id": "mark",
            "type": "update_record",
            "object": "lead",
            "record_id": "{{inputs.lead_id}}",
            "values": {"status": "working"},
        },
    ],
}


def run_worker(crm):
    def connect():
        return psycopg.connect(DATABASE_URL, autocommit=True)

    worker = Worker(default_backend(DATABASE_URL, connect), lambda: [crm.tenant], connect)
    return asyncio.run(worker.run_until_idle())


def test_ai_step_uses_tools_then_submits_and_the_run_continues(ai_ready, fake):
    crm = ai_ready
    crm.workflows.save(ASSESS)
    lead = crm.records.create("lead", {"family_name": "Doe", "company_name": "Acme"})
    fake["client"] = FakeClient(
        [
            reply(
                text("Checking duplicates."),
                tool(
                    "query_records",
                    {
                        "query": {
                            "object": "lead",
                            "filters": [{"field": "company_name", "value": "Acme"}],
                        }
                    },
                ),
            ),
            reply(tool("submit_result", {"decision": "pursue", "reason": "No duplicates."})),
        ]
    )
    run = crm.workflows.start("assess-lead", {"lead_id": lead["id"]})
    assert run["status"] == "queued"  # AI steps always run in the worker
    run_worker(crm)
    waiting = crm.workflows.get_run(run["run_id"])
    assert waiting["status"] == "waiting", waiting["error"]
    assert waiting["waiting_for"]["message"] == "AI says pursue: No duplicates."
    assert waiting["steps"]["assess"]["turns"] == 2
    assert fake["keys"] == ["sk-test-secret"]
    first = fake["client"].requests[0]
    assert first["model"] == MODEL and first["output_config"] == {"effort": "medium"}
    assert [t["name"] for t in first["tools"]] == ["query_records", "submit_result"]
    assert first["fallbacks"] == "default"
    assert "Acme" in first["messages"][0]["content"]
    tool_result = fake["client"].requests[1]["messages"][-1]["content"][0]
    assert json.loads(tool_result["content"])["count"] == 1
    status = AIService(crm).status()
    assert status["tokens_this_month"] == 240 and "sk-test" not in json.dumps(status)


def test_invalid_result_is_sent_back_for_correction(ai_ready, fake):
    crm = ai_ready
    crm.workflows.save(ASSESS)
    lead = crm.records.create("lead", {"family_name": "Doe"})
    fake["client"] = FakeClient(
        [
            reply(tool("submit_result", {"decision": "maybe", "reason": "x"})),
            reply(tool("submit_result", {"decision": "drop", "reason": "Too small."})),
        ]
    )
    run = crm.workflows.start("assess-lead", {"lead_id": lead["id"]})
    run_worker(crm)
    assert crm.workflows.get_run(run["run_id"])["status"] == "waiting"
    correction = fake["client"].requests[1]["messages"][-1]["content"][0]
    assert correction["is_error"] and "maybe" in correction["content"]


def test_tools_are_limited_to_the_listed_objects(ai_ready, fake):
    crm = ai_ready
    crm.workflows.save(ASSESS)
    lead = crm.records.create("lead", {"family_name": "Doe"})
    fake["client"] = FakeClient(
        [
            reply(
                tool("query_records", {"query": {"object": "app_user"}}),
                tool(
                    "update_record",
                    {"object": "lead", "record_id": lead["id"], "values": {"status": "qualified"}},
                    "c2",
                ),
            ),
            reply(tool("submit_result", {"decision": "drop", "reason": "n/a"})),
        ]
    )
    run = crm.workflows.start("assess-lead", {"lead_id": lead["id"]})
    run_worker(crm)
    results = fake["client"].requests[1]["messages"][-1]["content"]
    codes = [json.loads(r["content"])["code"] for r in results]
    assert codes == ["AI_TOOL_OBJECT_NOT_ALLOWED", "AI_TOOL_NOT_ALLOWED"]
    assert crm.records.get("lead", lead["id"])["status"] == "new"
    assert crm.workflows.get_run(run["run_id"])["status"] == "waiting"


def test_writes_use_the_run_principal_and_ai_provenance(ai_ready, fake, boot):
    crm = ai_ready
    crm.workflows.save(
        {
            "name": "ai-tag",
            "description": "Let AI set the status of a lead.",
            "inputs": {"lead_id": {"type": "string", "description": "The lead."}},
            "steps": [
                {
                    "id": "tag",
                    "type": "agent",
                    "instructions": "Set the status.",
                    "data": {"lead_id": "{{inputs.lead_id}}"},
                    "tools": ["update_record"],
                    "objects": ["lead"],
                    "output": {"done": {"type": "boolean", "description": "Done."}},
                }
            ],
        }
    )
    lead = crm.records.create("lead", {"family_name": "Doe"})
    fake["client"] = FakeClient(
        [
            reply(
                tool(
                    "update_record",
                    {"object": "lead", "record_id": lead["id"], "values": {"status": "working"}},
                )
            ),
            reply(tool("submit_result", {"done": True})),
        ]
    )
    agent = crm.as_principal(agent_for(boot.admin_user_id))
    run = agent.workflows.start("ai-tag", {"lead_id": lead["id"]})
    run_worker(crm)
    assert crm.workflows.get_run(run["run_id"])["status"] == "succeeded"
    change = crm.records.history("lead", lead["id"])[-1]
    assert change["new_value"] == "working" and change["source"] == "workflow"
    assert change["actor_type"] == "agent" and change["provenance"]["source_type"] == (
        "agent_inference"
    )


@pytest.mark.parametrize(
    ("script", "code"),
    [
        ([reply(text("no"), stop="refusal")], "AI_REFUSED"),
        ([reply(text("..."), stop="max_tokens")], "AI_OUTPUT_TRUNCATED"),
        ([reply(text("thinking"), stop="end_turn")] * 6, "AI_TURN_LIMIT"),
    ],
)
def test_ai_failures_fail_the_step(ai_ready, fake, script, code):
    crm = ai_ready
    crm.workflows.save(ASSESS)
    lead = crm.records.create("lead", {"family_name": "Doe"})
    fake["client"] = FakeClient(script)
    run = crm.workflows.start("assess-lead", {"lead_id": lead["id"]})
    run_worker(crm)
    failed = crm.workflows.get_run(run["run_id"])
    assert failed["status"] == "failed" and failed["error"]["code"] == code
    assert failed["error"]["step"] == "assess"


def test_monthly_budget_stops_ai_steps(ai_ready, fake):
    crm = ai_ready
    AIService(crm).update({"monthly_token_limit": 10_000})
    AIService(crm).record_usage(
        None, "earlier", MODEL, SimpleNamespace(input_tokens=9_000, output_tokens=1_000)
    )
    crm.workflows.save(ASSESS)
    lead = crm.records.create("lead", {"family_name": "Doe"})
    fake["client"] = FakeClient([])
    run = crm.workflows.start("assess-lead", {"lead_id": lead["id"]})
    run_worker(crm)
    assert crm.workflows.get_run(run["run_id"])["error"]["code"] == "AI_BUDGET_EXCEEDED"
    assert fake["client"].requests == []


def test_not_configured(crm, fake):
    crm.workflows.save(ASSESS)
    lead = crm.records.create("lead", {"family_name": "Doe"})
    run = crm.workflows.start("assess-lead", {"lead_id": lead["id"]})
    run_worker(crm)
    assert crm.workflows.get_run(run["run_id"])["error"]["code"] == "AI_NOT_CONFIGURED"


def test_skill_text_is_given_to_the_model(ai_ready, fake):
    crm = ai_ready
    crm.skills.save(
        "---\nname: lead-rules\ndescription: How we judge leads. Use when "
        "assessing leads.\n---\nDrop leads without a company.\n"
    )
    crm.workflows.save(
        {
            **ASSESS,
            "steps": [
                {**s, "skill": "lead-rules"} if s["id"] == "assess" else s for s in ASSESS["steps"]
            ],
        }
    )
    lead = crm.records.create("lead", {"family_name": "Doe"})
    fake["client"] = FakeClient(
        [reply(tool("submit_result", {"decision": "drop", "reason": "No company."}))]
    )
    crm.workflows.start("assess-lead", {"lead_id": lead["id"]})
    run_worker(crm)
    assert "Drop leads without a company." in fake["client"].requests[0]["messages"][0]["content"]


def test_ai_settings_need_ai_manage_and_a_person(crm, broker, make_user, boot):
    developer = crm.as_principal(make_user("dev@example.com", "developer"))
    with pytest.raises(OsierError) as err:
        AIService(developer, broker).update({"model": MODEL}, api_key="k")
    assert err.value.code == "MISSING_PERMISSION"
    agent = crm.as_principal(agent_for(boot.admin_user_id))
    with pytest.raises(OsierError) as err:
        AIService(agent, broker).update({"model": MODEL}, api_key="k")
    assert err.value.code == "APPROVAL_REQUIRES_HUMAN"
    with pytest.raises(OsierError) as err:
        AIService(crm, broker).update({"monthly_token_limit": 5})
    assert err.value.code == "AI_SETTINGS_INVALID"


def test_the_key_is_stored_encrypted(ai_ready, conn):
    from psycopg import sql

    row = conn.execute(
        sql.SQL("SELECT secret_ciphertext, settings::text FROM {}").format(
            sql.Identifier(ai_ready.tenant.schema, "meta_ai_settings")
        )
    ).fetchone()
    assert b"sk-test-secret" not in bytes(row[0]) and "sk-test" not in row[1]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"tools": ["get_record"], "objects": []}, "list the objects"),
        ({"objects": ["lead"], "tools": []}, "needs 'tools'"),
        ({"objects": ["nope"]}, "nope"),
        ({"tools": ["delete_everything"]}, "not valid"),
        ({"output": {}}, "not valid"),
        ({"output": {"Bad Name": {"description": "x"}}}, "lowercase"),
        ({"max_turns": 50}, "not valid"),
    ],
)
def test_invalid_ai_steps(crm, change, message):
    step = {**ASSESS["steps"][1], **change}
    with pytest.raises(OsierError, match=message):
        crm.workflows.preview({**ASSESS, "steps": [ASSESS["steps"][0], step]})


def test_preview_says_it_uses_ai_tokens(crm):
    preview = crm.workflows.preview(ASSESS)
    assert any("AI step" in line and "AI tokens" in line for line in preview["steps"])
