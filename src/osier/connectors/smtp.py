"""SMTP connector: works with most email providers (Gmail and Microsoft 365 with app
passwords, SendGrid, Postmark, Mailgun SMTP relays, a local test server)."""

from __future__ import annotations

import smtplib
import ssl
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..errors import OsierError
from .base import Action, ConnectorType, ExecutionContext
from .email import SEND_EMAIL_DESCRIPTION, Email, SendEmail, build_message
from .netguard import check_destination

TIMEOUT_SECONDS = 20


class ConnectorError(OsierError):
    default_code = "CONNECTOR_ERROR"


class SmtpConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = Field(min_length=1, max_length=255)
    port: int = Field(default=587, ge=1, le=65535)
    security: Literal["starttls", "ssl", "none"] = "starttls"
    username: str | None = Field(default=None, max_length=320)
    from_address: Email
    from_name: str | None = Field(default=None, max_length=200)


class SmtpSecrets(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: str = Field(min_length=1, max_length=1000)


class SmtpConnector(ConnectorType):
    key = "smtp"
    version = 1
    label = "SMTP email"
    description = (
        "Sends email through any SMTP server: Gmail or Microsoft 365 (with an app "
        "password), or a relay such as SendGrid, Postmark, or Mailgun."
    )
    config_model = SmtpConfig
    secret_model = SmtpSecrets
    actions = (
        Action(
            name="send_email",
            description=SEND_EMAIL_DESCRIPTION,
            input_model=SendEmail,
            side_effect="external_send",
            capability="email.send",
        ),
    )

    def _connect(self, ctx: ExecutionContext) -> smtplib.SMTP:
        config: SmtpConfig = ctx.config  # type: ignore[assignment]
        if config.security == "none" and not ctx.allow_private_network:
            raise ConnectorError(
                "Unencrypted SMTP is only allowed for local development.",
                code="INSECURE_TRANSPORT",
            )
        check_destination(config.host, config.port, allow_private_network=ctx.allow_private_network)
        try:
            if config.security == "ssl":
                server: smtplib.SMTP = smtplib.SMTP_SSL(
                    config.host,
                    config.port,
                    timeout=TIMEOUT_SECONDS,
                    context=ssl.create_default_context(),
                )
            else:
                server = smtplib.SMTP(config.host, config.port, timeout=TIMEOUT_SECONDS)
                if config.security == "starttls":
                    server.starttls(context=ssl.create_default_context())
            if config.username:
                password = ctx.secrets.password if ctx.secrets else ""  # type: ignore[union-attr]
                server.login(config.username, password)
            return server
        except smtplib.SMTPAuthenticationError:
            raise ConnectorError(
                "The SMTP server rejected the username or password.",
                code="CONNECTOR_AUTH_FAILED",
                hint="For Gmail, use an app password (requires 2-step verification).",
            ) from None
        except (OSError, smtplib.SMTPException) as exc:
            raise ConnectorError(
                f"Could not connect to the SMTP server: {exc.__class__.__name__}.",
                code="CONNECTOR_UNAVAILABLE",
                retryable=True,
            ) from None

    def verify(self, ctx: ExecutionContext) -> None:
        server = self._connect(ctx)
        try:
            server.noop()
        finally:
            server.quit()

    def execute(self, action: Action, arguments: SendEmail, ctx: ExecutionContext) -> dict:
        config: SmtpConfig = ctx.config  # type: ignore[assignment]
        message = build_message(
            arguments, from_address=config.from_address, from_name=config.from_name
        )
        server = self._connect(ctx)
        try:
            refused = server.send_message(message)
        except smtplib.SMTPRecipientsRefused:
            raise ConnectorError(
                "The SMTP server refused all recipients.", code="RECIPIENTS_REFUSED"
            ) from None
        except smtplib.SMTPException as exc:
            raise ConnectorError(
                f"Sending failed: {exc.__class__.__name__}.", code="SEND_FAILED", retryable=True
            ) from None
        finally:
            try:
                server.quit()
            except smtplib.SMTPException:
                pass
        return {
            "message_id": message["Message-ID"],
            "from": config.from_address,
            "refused_recipients": sorted(refused),
        }
