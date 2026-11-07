"""Credential broker: encrypts connector secrets stored in the tenant schema.

Keys are held outside the database (ARCHITECTURE.md §13.6). In development the key
comes from ``OSIER_SECRET_KEY`` or a key file; in production it will come from a
key management service behind this same interface.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from ..errors import OsierError


class CredentialError(OsierError):
    default_code = "CREDENTIAL_ERROR"


class CredentialBroker:
    def __init__(self, key: bytes) -> None:
        try:
            self._fernet = Fernet(key)
        except (ValueError, TypeError):
            raise CredentialError("The secret key is not a valid Fernet key.") from None
        self.key_id = hashlib.sha256(key).hexdigest()[:12]

    @staticmethod
    def generate_key() -> bytes:
        return Fernet.generate_key()

    @classmethod
    def from_environment(cls, key_file: Path | None = None) -> CredentialBroker:
        value = os.environ.get("OSIER_SECRET_KEY")
        if value:
            return cls(value.encode())
        if key_file is not None and key_file.exists():
            return cls(key_file.read_bytes().strip())
        raise CredentialError(
            "No secret key configured.",
            hint="Set OSIER_SECRET_KEY or run 'osier dev init' to create a local key file.",
        )

    def encrypt(self, secrets: dict[str, Any]) -> bytes:
        return self._fernet.encrypt(json.dumps(secrets).encode())

    def decrypt(self, ciphertext: bytes, key_id: str | None) -> dict[str, Any]:
        if key_id is not None and key_id != self.key_id:
            raise CredentialError(
                "These credentials were encrypted with a different key.",
                code="CREDENTIAL_KEY_MISMATCH",
                hint="Reconnect the connection, or configure the key it was created with.",
            )
        try:
            return json.loads(self._fernet.decrypt(bytes(ciphertext)))
        except InvalidToken:
            raise CredentialError("Credentials could not be decrypted.") from None


def encode_key_for_display(key: bytes) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(key).digest()[:6]).decode()
