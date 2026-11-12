"""Encryption at rest for broker credentials (Fernet: AES-128-CBC + HMAC-SHA256)."""
from __future__ import annotations

import json

from cryptography.fernet import Fernet, InvalidToken


class VaultError(Exception):
    pass


class CredentialVault:
    def __init__(self, key: str) -> None:
        self._key = key

    def _fernet(self) -> Fernet:
        if not self._key:
            raise VaultError("QAT_CREDENTIAL_KEY is not configured; cannot store or read broker credentials")
        try:
            return Fernet(self._key.encode())
        except ValueError as exc:
            raise VaultError("QAT_CREDENTIAL_KEY is not a valid Fernet key") from exc

    def encrypt(self, secrets: dict[str, str]) -> str:
        return self._fernet().encrypt(json.dumps(secrets).encode()).decode()

    def decrypt(self, ciphertext: str) -> dict[str, str]:
        try:
            return json.loads(self._fernet().decrypt(ciphertext.encode()))
        except InvalidToken:
            raise VaultError("Credential could not be decrypted (wrong key or corrupted data)") from None

    @staticmethod
    def generate_key() -> str:
        return Fernet.generate_key().decode()
