"""Encrypt imported passwords before writing account records."""

from __future__ import annotations

import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken


def _cipher() -> Fernet:
    secret = os.getenv("KR_CREDENTIAL_KEY") or os.getenv("KR_ADMIN_TOKEN") or ""
    if not secret:
        raise ValueError("KR_CREDENTIAL_KEY or KR_ADMIN_TOKEN is required for password import")
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
    return Fernet(key)


def encrypt_password(password: str) -> str:
    if not password:
        raise ValueError("password is required")
    return _cipher().encrypt(password.encode("utf-8")).decode("ascii")


def decrypt_password(ciphertext: str) -> str:
    try:
        return _cipher().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("stored password cannot be decrypted; check KR_CREDENTIAL_KEY") from exc
