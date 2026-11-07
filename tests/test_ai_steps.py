"""AI steps in workflows, over the OpenAI-compatible API with scripted responses (no
network, no spend).

The fake client returns real ``openai`` ``ChatCompletion`` objects and raises real SDK
errors, so response parsing is tested against the real shapes."""

import asyncio
import json

import httpx2
import openai
import psycopg
import pytest
from conftest import DATABASE_URL, agent_for
from openai.types.chat import ChatCompletion

from osier import ai
from osier.ai import AIService
from osier.connectors.vault import CredentialBroker
from osier.errors import OsierError
from osier.jobs import Worker, default_backend


def response(*, text=None, tools=(), finish=None, refusal=None, usage=(10, 20), cost=None):
    """A ChatCompletion. tools: (name, args) pairs."""
    calls = [
        {
            "id": f"c{i}",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)},
        }
        for i, (name, args) in enumerate(tools)
    ]
    usage_data = {
        "prompt_tokens": usage[0],
        "completion_tokens": usage[1],
        "total_tokens": sum(usage),
    }
    if cost is not None:
        usage_data["cost"] = cost  # what OpenRouter adds
    return ChatCompletion.model_validate(
        {
            "id": "r",
            "object": "chat.completion",
            "created": 0,
            "model": "m",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": finish or ("tool_calls" if calls else "stop"),
                    "message": {
                        "role": "assistant",
                        "content": text,
                        "tool_calls": calls or None,
                        "refusal": refusal,
                    },
                }
            ],
            "usage": usage_data,
        }
    )


def sdk_error(cls):
    request = httpx2.Request("POST", "https://llm.example/v1/chat/completions")
    return cls("nope", response=httpx2.Response(401, request=request), body=None)


class FakeClient:
    """Each script entry: a list of (tool, args) pairs, a str (text reply), a dict of
    ``response`` keywords, a ChatCompletion, or an exception to raise."""

    def __init__(self, *script):
        self.script = list(script)
        self.requests = []
        self.chat = type("Chat", (), {"completions": self})()

    def create(self, **request):
        self.requests.append({**request, "messages": [dict(m) for m in request["messages"]]})
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        if isinstance(step, ChatCompletion):
            return step
        if isinstance(step, dict):
            return response(**step)
        if isinstance(step, str):
            return response(text=step)
        return response(tools=step)


@pytest.fixture
def broker():
    return CredentialBroker(CredentialBroker.generate_key())


@pytest.fixture
def fake(broker):
    holder = {"client": None, "connections": []}

    def factory(api_key, base_url):
        holder["connections"].append((api_key, base_url))
        return holder["client"]

    ai.configure_runtime(broker=broker, client_factory=factory)
    yield holder
    ai.configure_runtime(broker=None, client_factory=None)


@pytest.fixture
def ai_ready(crm, broker, fake, monkeypatch):
    import osier.connectors.netguard as netguard

    monkeypatch.setattr(netguard, "check_destination", lambda *a, **k: None)  # no DNS in tests
    service = AIService(crm, broker)
    service.connect(
        "main",
        provider="openrouter",
        models=["vendor/big", "vendor/small"],
        api_key="sk-test-secret",
        input_usd_per_million="2",
        output_usd_per_million="10",
    )
    service.connect(
        "local",
        provider="compatible",
        models=["local-model"],
        api_key=None,
        api_base="https://llm.internal.example.com/v1",
    )
    service.set_profile(
        name="careful", connection="main", model="vendor/big", effort="high", default=True
    )
    service.set_profile(name="fast", connection="main", model="vendor/small", effort="low")
    service.set_profile(name="private", connection="local", model="local-model")
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
            (
                "query_records",
                {
                    "query": {
                        "object": "lead",
                        "filters": [{"field": "company_name", "value": "Acme"}],
                    }
                },
            )
        ],
        [("submit_result", {"decision": "pursue", "reason": "No duplicates."})],
    )
    run = crm.workflows.start("assess-lead", {"lead_id": lead["id"]})
    assert run["status"] == "queued"  # AI steps always run in the worker
    run_worker(crm)
    waiting = crm.workflows.get_run(run["run_id"])
    assert waiting["status"] == "waiting", waiting["error"]
    assert waiting["waiting_for"]["message"] == "AI says pursue: No duplicates."
    assess = waiting["steps"]["assess"]
    assert assess["turns"] == 2 and assess["model_profile"] == "careful"
    first, second = fake["client"].requests
    assert fake["connections"][0] == ("sk-test-secret", "https://openrouter.ai/api/v1")
    assert first["model"] == "vendor/big" and first["reasoning_effort"] == "high"
    assert first["max_tokens"] == 16_000 and "max_completion_tokens" not in first
    assert first["tools"][-1]["function"]["strict"] is True
    assert [t["function"]["name"] for t in first["tools"]] == ["query_records", "submit_result"]
    assert "Acme" in first["messages"][1]["content"]
    tool_message = second["messages"][-1]
    assert tool_message["role"] == "tool" and json.loads(tool_message["content"])["count"] == 1
    # The fake reports 10 input and 20 output tokens per call; prices are 2/10 USD per M.
    usage = assess["usage"]
    assert usage["input_tokens"] == 20 and usage["output_tokens"] == 40
    assert usage["cost_usd"] == pytest.approx((20 * 2 + 40 * 10) / 1_000_000)


def test_steps_can_use_different_models(ai_ready, fake):
    crm = ai_ready
    two = {
        **ASSESS,
        "steps": [
            ASSESS["steps"][0],
            {**ASSESS["steps"][1], "id": "quick", "model": "fast", "tools": [], "objects": []},
            {**ASSESS["steps"][1], "id": "local", "model": "private", "tools": [], "objects": []},
        ],
    }
    crm.workflows.save(two)
    lead = crm.records.create("lead", {"family_name": "Doe"})
    fake["client"] = FakeClient(
        [("submit_result", {"decision": "drop", "reason": "a"})],
        [("submit_result", {"decision": "pursue", "reason": "b"})],
    )
    run = crm.workflows.start("assess-lead", {"lead_id": lead["id"]})
    run_worker(crm)
    assert crm.workflows.get_run(run["run_id"])["status"] == "succeeded"
    quick, local = fake["client"].requests
    assert quick["model"] == "vendor/small" and quick["reasoning_effort"] == "low"
    assert local["model"] == "local-model" and "reasoning_effort" not in local  # none set
    assert "strict" not in local["tools"][-1]["function"]  # compatible servers: not assumed
    assert fake["connections"] == [
        ("sk-test-secret", "https://openrouter.ai/api/v1"),
        (None, "https://llm.internal.example.com/v1"),
    ]


def test_invalid_result_is_sent_back_for_correction(ai_ready, fake):
    crm = ai_ready
    crm.workflows.save(ASSESS)
    lead = crm.records.create("lead", {"family_name": "Doe"})
    fake["client"] = FakeClient(
        [("submit_result", {"decision": "maybe", "reason": "x"})],
        [("submit_result", {"decision": "drop", "reason": "Too small."})],
    )
    run = crm.workflows.start("assess-lead", {"lead_id": lead["id"]})
    run_worker(crm)
    assert crm.workflows.get_run(run["run_id"])["status"] == "waiting"
    correction = fake["client"].requests[1]["messages"][-1]
    assert correction["role"] == "tool" and "maybe" in correction["content"]


def test_tools_are_limited_to_the_listed_objects(ai_ready, fake):
    crm = ai_ready
    crm.workflows.save(ASSESS)
    lead = crm.records.create("lead", {"family_name": "Doe"})
    fake["client"] = FakeClient(
        [
            ("query_records", {"query": {"object": "app_user"}}),
            (
                "update_record",
                {"object": "lead", "record_id": lead["id"], "values": {"status": "qualified"}},
            ),
        ],
        [("submit_result", {"decision": "drop", "reason": "n/a"})],
    )
    run = crm.workflows.start("assess-lead", {"lead_id": lead["id"]})
    run_worker(crm)
    tool_messages = fake["client"].requests[1]["messages"][-2:]
    codes = [json.loads(m["content"])["error"]["code"] for m in tool_messages]
    assert codes == ["AI_TOOL_OBJECT_NOT_ALLOWED", "AI_TOOL_NOT_ALLOWED"]
    assert crm.records.get("lead", lead["id"])["status"] == "new"
    assert crm.workflows.get_run(run["run_id"])["status"] == "waiting"


def test_writes_use_the_run_principal_and_ai_provenance(ai_ready, fake, boot):
    crm = ai_ready
    crm.workflows.save(
        {
            "name": "ai-status",
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
            (
                "update_record",
                {"object": "lead", "record_id": lead["id"], "values": {"status": "working"}},
            )
        ],
        [("submit_result", {"done": True})],
    )
    agent = crm.as_principal(agent_for(boot.admin_user_id))
    run = agent.workflows.start("ai-status", {"lead_id": lead["id"]})
    run_worker(crm)
    assert crm.workflows.get_run(run["run_id"])["status"] == "succeeded"
    change = crm.records.history("lead", lead["id"])[-1]
    assert change["new_value"] == "working" and change["source"] == "workflow"
    assert change["actor_type"] == "agent"
    assert change["provenance"]["source_type"] == "agent_inference"


@pytest.mark.parametrize(
    ("script", "code"),
    [
        ([{"finish": "content_filter"}], "AI_REFUSED"),
        ([{"text": "I can't help with that.", "refusal": "I can't help with that."}], "AI_REFUSED"),
        ([{"finish": "length"}], "AI_OUTPUT_TRUNCATED"),
        (["thinking out loud"] * 6, "AI_TURN_LIMIT"),
        (["auth"], "AI_REQUEST_FAILED"),
    ],
)
def test_ai_failures_fail_the_step(ai_ready, fake, script, code):
    crm = ai_ready
    crm.workflows.save(ASSESS)
    lead = crm.records.create("lead", {"family_name": "Doe"})
    if script == ["auth"]:
        script = [sdk_error(openai.AuthenticationError)]
    fake["client"] = FakeClient(*script)
    run = crm.workflows.start("assess-lead", {"lead_id": lead["id"]})
    run_worker(crm)
    failed = crm.workflows.get_run(run["run_id"])
    assert failed["status"] == "failed" and failed["error"]["code"] == code
    assert failed["error"]["step"] == "assess"


def test_openai_requests_use_max_completion_tokens(ai_ready, fake, broker):
    crm = ai_ready
    AIService(crm, broker).connect("oa", provider="openai", models=["gpt-x"], api_key="sk-oa")
    AIService(crm).set_profile(name="oa-profile", connection="oa", model="gpt-x")
    step = {**ASSESS["steps"][1], "model": "oa-profile", "tools": [], "objects": []}
    crm.workflows.save({**ASSESS, "steps": [ASSESS["steps"][0], step, *ASSESS["steps"][2:]]})
    lead = crm.records.create("lead", {"family_name": "Doe"})
    fake["client"] = FakeClient([("submit_result", {"decision": "drop", "reason": "x"})])
    crm.workflows.start("assess-lead", {"lead_id": lead["id"]})
    run_worker(crm)
    request = fake["client"].requests[0]
    assert request["max_completion_tokens"] == 16_000 and "max_tokens" not in request
    assert fake["connections"][-1] == ("sk-oa", "https://api.openai.com/v1")
    assert "reasoning_effort" not in request  # the profile sets none


def test_openrouter_reported_cost_is_used_without_prices(ai_ready, fake, broker):
    crm = ai_ready
    service = AIService(crm, broker)
    service.connect(
        "or2", provider="openrouter", models=["m"], api_key="k", monthly_budget_usd="5"
    )  # allowed: OpenRouter reports costs
    service.set_profile(name="or2-profile", connection="or2", model="m")
    resolved = service.resolve("or2-profile")
    used = service.record_usage(resolved, None, "s", response(usage=(100, 50), cost=0.0042))
    assert used["cost_usd"] == pytest.approx(0.0042)
    # an explicit price on the connection wins over what the provider reports
    priced = service.resolve("careful")
    used = service.record_usage(priced, None, "s", response(usage=(1_000_000, 0), cost=99))
    assert used["cost_usd"] == pytest.approx(2.0)


def test_dollar_budgets_need_a_cost_source(crm, broker, monkeypatch):
    import osier.connectors.netguard as netguard

    monkeypatch.setattr(netguard, "check_destination", lambda *a, **k: None)
    service = AIService(crm, broker)
    with pytest.raises(OsierError) as err:
        service.connect("x", provider="openai", models=["m"], api_key="k", monthly_budget_usd="10")
    assert err.value.code == "AI_BUDGET_NEEDS_PRICES"
    service.connect(
        "x",
        provider="openai",
        models=["m"],
        api_key="k",
        monthly_budget_usd="10",
        input_usd_per_million="1",
        output_usd_per_million="4",
    )
    service.connect("y", provider="openai", models=["m"], api_key="k")
    with pytest.raises(OsierError) as err:
        service.update_connection("y", {"monthly_budget_usd": "10"})
    assert err.value.code == "AI_BUDGET_NEEDS_PRICES"
    service.update_connection("x", {"input_usd_per_million": "0", "output_usd_per_million": "0"})


def test_monthly_limits_per_connection(ai_ready, fake):
    crm = ai_ready
    service = AIService(crm)
    service.update_connection("main", {"monthly_budget_usd": "0.000001"})
    resolved = service.resolve("careful")
    service.record_usage(resolved, None, "earlier", response(usage=(10, 20)))
    crm.workflows.save(ASSESS)
    lead = crm.records.create("lead", {"family_name": "Doe"})
    fake["client"] = FakeClient()
    run = crm.workflows.start("assess-lead", {"lead_id": lead["id"]})
    run_worker(crm)
    assert crm.workflows.get_run(run["run_id"])["error"]["code"] == "AI_BUDGET_EXCEEDED"
    assert fake["client"].requests == []


def test_saving_needs_an_existing_profile(crm):
    with pytest.raises(OsierError) as err:
        crm.workflows.preview(ASSESS)
    assert err.value.code == "AI_NOT_CONFIGURED"


def test_unknown_profile_is_refused(ai_ready):
    bad = {**ASSESS, "steps": [ASSESS["steps"][0], {**ASSESS["steps"][1], "model": "nope"}]}
    with pytest.raises(OsierError) as err:
        ai_ready.workflows.preview(bad)
    assert err.value.code == "AI_PROFILE_NOT_FOUND"


def test_profiles_only_use_models_the_connection_allows(ai_ready):
    with pytest.raises(OsierError) as err:
        AIService(ai_ready).set_profile(name="x", connection="main", model="vendor/other")
    assert err.value.code == "AI_MODEL_NOT_ALLOWED"


def test_unknown_provider_and_plain_http_are_refused(crm, broker):
    service = AIService(crm, broker)
    for provider in ("made-up", "anthropic", "azure"):
        with pytest.raises(OsierError) as err:
            service.connect("x", provider=provider, models=["m"], api_key="k")
        assert err.value.code == "AI_PROVIDER_UNKNOWN"
        assert err.value.expected == ["compatible", "openai", "openrouter"]
    with pytest.raises(OsierError, match="needs an API base URL"):
        service.connect("x", provider="compatible", models=["m"], api_key="k")
    with pytest.raises(OsierError, match="https"):
        service.connect(
            "x", provider="openai", models=["m"], api_key="k", api_base="http://example.com/v1"
        )


def test_private_network_api_base_is_refused(crm, broker):
    with pytest.raises(OsierError):
        AIService(crm, broker).connect(
            "x", provider="openai", models=["m"], api_key="k", api_base="https://127.0.0.1/v1"
        )


def test_connections_in_use_cannot_be_removed(ai_ready):
    with pytest.raises(OsierError) as err:
        AIService(ai_ready).disconnect("main")
    assert err.value.code == "AI_CONNECTION_IN_USE"


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
    fake["client"] = FakeClient([("submit_result", {"decision": "drop", "reason": "No company."})])
    crm.workflows.start("assess-lead", {"lead_id": lead["id"]})
    run_worker(crm)
    assert "Drop leads without a company." in fake["client"].requests[0]["messages"][1]["content"]


def test_ai_settings_need_ai_manage_and_a_person(crm, broker, make_user, boot):
    developer = crm.as_principal(make_user("dev@example.com", "developer"))
    with pytest.raises(OsierError) as err:
        AIService(developer, broker).connect("x", provider="openai", models=["m"], api_key="k")
    assert err.value.code == "MISSING_PERMISSION"
    agent = crm.as_principal(agent_for(boot.admin_user_id))
    with pytest.raises(OsierError) as err:
        AIService(agent, broker).connect("x", provider="openai", models=["m"], api_key="k")
    assert err.value.code == "APPROVAL_REQUIRES_HUMAN"


def test_keys_are_stored_encrypted_and_never_shown(ai_ready, conn):
    from psycopg import sql

    rows = conn.execute(
        sql.SQL("SELECT secret_ciphertext, settings::text FROM {}").format(
            sql.Identifier(ai_ready.tenant.schema, "ai_connections")
        )
    ).fetchall()
    for ciphertext, settings in rows:
        assert ciphertext is None or b"sk-test-secret" not in bytes(ciphertext)
        assert "sk-test" not in settings
    assert "sk-test" not in json.dumps(AIService(ai_ready).status(), default=str)


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
def test_invalid_ai_steps(ai_ready, change, message):
    step = {**ASSESS["steps"][1], **change}
    with pytest.raises(OsierError, match=message):
        ai_ready.workflows.preview({**ASSESS, "steps": [ASSESS["steps"][0], step]})


def test_preview_says_which_model_profile(ai_ready):
    preview = ai_ready.workflows.preview(ASSESS)
    line = next(x for x in preview["steps"] if "AI step" in x)
    assert "default model profile" in line and "AI tokens" in line
