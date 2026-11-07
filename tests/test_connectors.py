import pytest
from psycopg import sql

from osier.connectors import ConnectorService, CredentialBroker
from osier.connectors import smtp as smtp_module
from osier.errors import ApprovalError, IntegrityError, NotFoundError, OsierError

KEY = CredentialBroker.generate_key()


class FakeSMTP:
    sent: list = []
    logins: list = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host = host

    def starttls(self, context=None):
        pass

    def login(self, user, password):
        if password != "app-password":
            import smtplib

            raise smtplib.SMTPAuthenticationError(535, b"bad")
        FakeSMTP.logins.append(user)

    def noop(self):
        pass

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)
        return {}

    def quit(self):
        pass


@pytest.fixture(autouse=True)
def fake_smtp(monkeypatch):
    FakeSMTP.sent, FakeSMTP.logins = [], []
    monkeypatch.setattr(smtp_module.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtp_module, "check_destination", lambda *a, **k: None)


@pytest.fixture
def admin_connectors(crm):
    return ConnectorService(crm, CredentialBroker(KEY))


@pytest.fixture
def agent_connectors(agent):
    return ConnectorService(agent, CredentialBroker(KEY))


SMTP_CONFIG = {
    "host": "smtp.example.com",
    "port": 587,
    "username": "sales@acme.test",
    "from_address": "sales@acme.test",
    "from_name": "Acme Sales",
}


def connect_smtp(svc, **kw):
    return svc.create_connection(
        "smtp",
        kw.pop("name", "Sales SMTP"),
        config=SMTP_CONFIG,
        secrets={"password": "app-password"},
        **kw,
    )


def email_args(**kw):
    return {
        "to": ["jane@example.com"],
        "subject": "Following up",
        "body_text": "Hi Jane, thanks for your time.",
        **kw,
    }


def test_integrations_package_is_installed_with_connection_object(crm):
    obj = crm.snapshot().get_object("connection")
    assert obj.access == "engine_managed" and obj.managed_by == "connectors"


def test_create_connection_stores_encrypted_credentials(admin_connectors, crm, conn):
    connection = connect_smtp(admin_connectors)
    assert connection["status"] == "active" and connection["capabilities"] == ["email.send"]
    row = conn.execute(
        sql.SQL("SELECT config, secret_ciphertext FROM {} WHERE connection_id = %s").format(
            sql.Identifier(crm.tenant.schema, "connector_credentials")
        ),
        (connection["id"],),
    ).fetchone()
    assert row[0]["host"] == "smtp.example.com"
    assert b"app-password" not in bytes(row[1])  # encrypted at rest
    record = crm.records.get("connection", connection["id"])
    assert "password" not in str(record)


def test_wrong_password_fails_verification(admin_connectors):
    with pytest.raises(OsierError) as exc:
        admin_connectors.create_connection(
            "smtp", "Bad", config=SMTP_CONFIG, secrets={"password": "wrong"}
        )
    assert exc.value.code == "CONNECTOR_AUTH_FAILED"
    assert admin_connectors.list_connections() == []


def test_agents_cannot_create_connections(agent_connectors):
    with pytest.raises(ApprovalError) as exc:
        connect_smtp(agent_connectors)
    assert exc.value.code == "APPROVAL_REQUIRES_HUMAN"


def test_connection_records_cannot_be_written_directly(admin_connectors, crm):
    connection = connect_smtp(admin_connectors)
    with pytest.raises(ApprovalError) as exc:
        crm.records.update(
            "connection", connection["id"], {"status": "revoked"}, expected_row_version=1
        )
    assert exc.value.code == "ENGINE_MANAGED_OBJECT"


def test_send_needs_human_approval_then_sends_and_logs_activity(
    admin_connectors, agent_connectors, agent
):
    connect_smtp(admin_connectors)
    lead = agent.records.create("lead", {"family_name": "Doe", "email": "jane@example.com"})
    call = agent_connectors.send_email(
        email_args(related_object="lead", related_record_id=lead["id"])
    )
    assert call["status"] == "awaiting_approval" and FakeSMTP.sent == []
    assert "approve" in call["next_step"]
    with pytest.raises(ApprovalError):
        agent_connectors.approve(call["call_id"])  # agents cannot approve

    done = admin_connectors.approve(call["call_id"])
    assert done["status"] == "succeeded"
    assert FakeSMTP.sent[0]["To"] == "jane@example.com"
    assert FakeSMTP.sent[0]["From"] == "Acme Sales <sales@acme.test>"
    activities = agent.records.list("activity", where={"lead": lead["id"]})
    assert activities[0]["activity_type"] == "email"
    assert activities[0]["subject"] == "Email: Following up"
    assert activities[0]["created_by"] == str(agent.principal.user_id)  # requester, not approver
    assert done["result"]["logged_as"]["object"] == "activity"


def test_reject(admin_connectors, agent_connectors):
    connect_smtp(admin_connectors)
    call = agent_connectors.send_email(email_args())
    assert admin_connectors.reject(call["call_id"])["status"] == "rejected"
    with pytest.raises(ApprovalError):
        admin_connectors.approve(call["call_id"])
    assert FakeSMTP.sent == []


def test_dev_outbox_with_auto_approval(admin_connectors, agent_connectors):
    admin_connectors.create_connection("dev_outbox", "Outbox", approval="not_required")
    call = agent_connectors.send_email(email_args())
    assert call["status"] == "succeeded"
    assert call["result"]["delivered"] is False


def test_routing_named_default_and_ambiguous(admin_connectors, agent_connectors):
    connect_smtp(admin_connectors, name="Sales SMTP")
    admin_connectors.create_connection("dev_outbox", "Outbox", approval="not_required")
    with pytest.raises(IntegrityError) as exc:
        agent_connectors.send_email(email_args())
    assert exc.value.code == "CONNECTION_AMBIGUOUS"
    assert sorted(exc.value.expected) == ["Outbox", "Sales SMTP"]
    named = agent_connectors.send_email(email_args(), connection="Outbox")
    assert named["status"] == "succeeded"
    with pytest.raises(NotFoundError):
        agent_connectors.send_email(email_args(), connection="Nope")


def test_default_connection_is_used(admin_connectors, agent_connectors):
    connect_smtp(admin_connectors, name="Sales SMTP")
    admin_connectors.create_connection(
        "dev_outbox", "Outbox", approval="not_required", is_default=True
    )
    assert agent_connectors.send_email(email_args())["status"] == "succeeded"


def test_no_connection(agent_connectors):
    with pytest.raises(NotFoundError) as exc:
        agent_connectors.send_email(email_args())
    assert exc.value.code == "NO_CONNECTION"


def test_user_scoped_connections_are_private(admin_connectors, crm, make_user):
    connect_smtp(admin_connectors, scope="user")
    other = ConnectorService(
        crm.as_principal(make_user("bob@acme.test", "member")), CredentialBroker(KEY)
    )
    assert other.list_connections() == []
    assert len(admin_connectors.list_connections()) == 1


@pytest.mark.parametrize(
    ("args", "code"),
    [
        ({"to": [f"p{i}@x.com" for i in range(11)]}, "CONNECTOR_INPUT_INVALID"),
        ({"to": ["not-an-email"]}, "CONNECTOR_INPUT_INVALID"),
        ({"purpose": "marketing"}, "NOT_SUPPORTED_YET"),
        ({"related_object": "lead"}, "VALIDATION_FAILED"),
        ({"bcc": ["x@y.com"]}, "CONNECTOR_INPUT_INVALID"),
    ],
)
def test_invalid_sends(admin_connectors, agent_connectors, args, code):
    admin_connectors.create_connection("dev_outbox", "Outbox", approval="not_required")
    admin_connectors.create_connection(
        "dev_outbox", "Mk", purpose="marketing", approval="not_required"
    )
    with pytest.raises(OsierError) as exc:
        agent_connectors.send_email(email_args(**args))
    assert exc.value.code == code


def test_recipient_limit_from_policy(admin_connectors, agent_connectors):
    admin_connectors.create_connection(
        "dev_outbox", "Outbox", approval="not_required", max_recipients=1
    )
    with pytest.raises(OsierError) as exc:
        agent_connectors.send_email(email_args(to=["a@x.com", "b@x.com"]))
    assert exc.value.code == "RECIPIENT_LIMIT"


def test_idempotent_sends(admin_connectors, agent_connectors):
    admin_connectors.create_connection("dev_outbox", "Outbox", approval="not_required")
    first = agent_connectors.send_email(email_args(), idempotency_key="k1")
    again = agent_connectors.send_email(email_args(), idempotency_key="k1")
    assert first["call_id"] == again["call_id"]


def test_failed_send_marks_connection_error(admin_connectors, agent_connectors, monkeypatch, crm):
    connection = connect_smtp(admin_connectors)

    def boom(self, msg):
        import smtplib

        raise smtplib.SMTPServerDisconnected("gone")

    monkeypatch.setattr(FakeSMTP, "send_message", boom)
    call = agent_connectors.send_email(email_args())
    done = admin_connectors.approve(call["call_id"])
    assert done["status"] == "failed" and done["error"]["code"] == "SEND_FAILED"
    assert crm.records.get("connection", connection["id"])["status"] == "error"


def test_wrong_key_cannot_decrypt(crm, admin_connectors, agent):
    connect_smtp(admin_connectors)
    other = ConnectorService(crm, CredentialBroker(CredentialBroker.generate_key()))
    call = ConnectorService(agent, CredentialBroker(KEY)).send_email(email_args())
    done = other.approve(call["call_id"])
    assert done["status"] == "failed" and done["error"]["code"] == "CREDENTIAL_KEY_MISMATCH"


def test_private_network_destinations_are_blocked():
    from osier.connectors.netguard import NetworkPolicyError, check_destination

    for host in ("127.0.0.1", "10.0.0.5", "169.254.169.254", "192.168.1.1"):
        with pytest.raises(NetworkPolicyError):
            check_destination(host, 25, allow_private_network=False)
    check_destination("127.0.0.1", 25, allow_private_network=True)
