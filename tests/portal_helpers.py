"""Helpers shared by the portal tests."""

import re

KEY = "test-launch-key"
BASE = "http://127.0.0.1:8000"


def csrf_of(html: str) -> str:
    match = re.search(r'name="csrf" value="([^"]+)"', html)
    assert match, "no CSRF field on the page"
    return match.group(1)


def sign_in(client, user_id):
    page = client.get(f"/login?key={KEY}")
    assert page.status_code == 200
    response = client.post("/login", data={"key": KEY, "user": str(user_id)})
    assert response.status_code == 303 and response.headers["location"] == "/"
    return response


def post(client, path, **data):
    token = csrf_of(client.get("/").text)
    return client.post(path, data={"csrf": token, **data})


PASSWORD = "app-password"


class FakeSMTP:
    """A stand-in SMTP server: accepts PASSWORD, records what is sent."""

    sent: list = []

    def __init__(self, host, port, timeout=None, context=None):
        pass

    def starttls(self, context=None):
        pass

    def login(self, user, password):
        if password != PASSWORD:
            import smtplib

            raise smtplib.SMTPAuthenticationError(535, b"bad")

    def noop(self):
        pass

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)
        return {}

    def quit(self):
        pass


def email_args(**kw):
    return {
        "to": ["jane@example.com"],
        "subject": "Following up",
        "body_text": "Hi Jane, thanks for your time.",
        **kw,
    }


def agent_for(user_id):
    """An agent acting for the given person."""
    import uuid

    from osier.context import ActorType, Principal

    return Principal(
        user_id=uuid.UUID("00000000-0000-0000-0000-00000000b001"),
        actor_type=ActorType.AGENT,
        on_behalf_of=user_id,
        agent_id="test-agent",
        source="chat",
    )
