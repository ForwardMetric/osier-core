"""Development outbox: behaves like an email connector but sends nothing.

Messages are recorded in the connector call audit, so agents can be tested safely.
"""

from __future__ import annotations

from email.utils import make_msgid

from pydantic import BaseModel, ConfigDict

from .base import Action, ConnectorType, ExecutionContext
from .email import SEND_EMAIL_DESCRIPTION, Email, SendEmail


class OutboxConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_address: Email = "agent@example.test"


class DevOutboxConnector(ConnectorType):
    key = "dev_outbox"
    version = 1
    label = "Development outbox"
    description = (
        "Pretends to send email and records each message instead. For testing agents "
        "without emailing anyone."
    )
    config_model = OutboxConfig
    secret_model = None
    actions = (
        Action(
            name="send_email",
            description=SEND_EMAIL_DESCRIPTION + " (Development outbox: nothing is sent.)",
            input_model=SendEmail,
            side_effect="external_send",
            capability="email.send",
        ),
    )

    def execute(self, action: Action, arguments: SendEmail, ctx: ExecutionContext) -> dict:
        return {
            "message_id": make_msgid(domain="outbox.osier.test"),
            "from": ctx.config.from_address,  # type: ignore[attr-defined]
            "delivered": False,
            "note": "Recorded in the development outbox; nothing was sent.",
        }
