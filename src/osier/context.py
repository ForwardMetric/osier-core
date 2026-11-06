"""Who is acting, and in which tenant."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import StrEnum

from .identifiers import validate_tenant_schema


class ActorType(StrEnum):
    HUMAN = "human"
    AGENT = "agent"
    SYSTEM = "system"


SYSTEM_USER_ID = uuid.UUID(int=0)


@dataclass(frozen=True)
class Principal:
    """The identity a request runs as.

    ``user_id`` is the acting identity. When an agent acts for a person,
    ``actor_type`` is ``agent``, ``agent_id`` identifies the agent/client and
    ``on_behalf_of`` is the person's user id.
    """

    user_id: uuid.UUID
    actor_type: ActorType = ActorType.HUMAN
    on_behalf_of: uuid.UUID | None = None
    agent_id: str | None = None
    source: str = "api"  # e.g. chat, api, import, upgrade
    roles: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def system(cls, source: str = "platform") -> Principal:
        return cls(user_id=SYSTEM_USER_ID, actor_type=ActorType.SYSTEM, source=source)

    @property
    def is_human(self) -> bool:
        return self.actor_type is ActorType.HUMAN


@dataclass(frozen=True)
class Tenant:
    """Where a tenant lives. In M1 only the schema is used; the control plane
    (M5) will add cluster and database placement."""

    tenant_id: uuid.UUID
    schema: str

    def __post_init__(self) -> None:
        validate_tenant_schema(self.schema)
