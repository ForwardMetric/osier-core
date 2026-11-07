"""The ``email.send`` capability: shared input model and message building."""

from __future__ import annotations

import re
import uuid
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

_EMAIL_RE = re.compile(r"^[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+$")
MAX_TRANSACTIONAL_RECIPIENTS = 10


def _email(value: str) -> str:
    if not _EMAIL_RE.fullmatch(value):
        raise ValueError("not a valid email address")
    return value


Email = Annotated[str, Field(max_length=320), AfterValidator(_email)]


class SendEmail(BaseModel):
    """Send one email. Recipients must be people the message is genuinely for
    (one-to-one or a small group), not a list."""

    model_config = ConfigDict(extra="forbid")

    to: list[Email] = Field(min_length=1, max_length=MAX_TRANSACTIONAL_RECIPIENTS)
    cc: list[Email] = Field(default_factory=list, max_length=MAX_TRANSACTIONAL_RECIPIENTS)
    subject: str = Field(min_length=1, max_length=300)
    body_text: str = Field(min_length=1, max_length=100_000)
    reply_to: Email | None = None
    purpose: Literal["transactional", "marketing"] = "transactional"
    related_object: Literal["lead", "contact", "account", "opportunity", "support_case"] | None = (
        None
    )
    related_record_id: uuid.UUID | None = None

    @property
    def recipient_count(self) -> int:
        return len(self.to) + len(self.cc)


def build_message(args: SendEmail, *, from_address: str, from_name: str | None) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((from_name, from_address)) if from_name else from_address
    msg["To"] = ", ".join(args.to)
    if args.cc:
        msg["Cc"] = ", ".join(args.cc)
    if args.reply_to:
        msg["Reply-To"] = args.reply_to
    msg["Subject"] = args.subject
    msg["Message-ID"] = make_msgid(domain=from_address.split("@")[-1])
    msg.set_content(args.body_text)
    return msg


SEND_EMAIL_DESCRIPTION = (
    "Send one email to up to 10 recipients through the tenant's email connection. "
    "Use for one-to-one messages such as follow-ups and replies; not for newsletters. "
    "Pass related_object and related_record_id so the email is logged as an activity "
    "on that record. Sends usually need a person's approval: the result then has status "
    "'awaiting_approval' and nothing is sent until they approve."
)
