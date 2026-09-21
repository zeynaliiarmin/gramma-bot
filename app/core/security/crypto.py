"""
Gramma — symmetric encryption helpers (AES-256).

Instagram access tokens (and the Meta app secret) are **never** stored in
plain text. We use Fernet (AES-128-CBC + HMAC-SHA256 under the hood, 256-bit
key) so that tampering with a ciphertext is cryptographically detectable and
fails loudly instead of silently decrypting garbage.

Key rotation is forward-compatible: items store the key id they were
encrypted with (`key_version` column).
"""

from __future__ import annotations

import base64

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import get_settings


class Cipher:
    """Small wrapper around Fernet with explicit error types."""

    def __init__(self, key_b64: str) -> None:
        # Tolerate a raw base64 key that isn't already Fernet-prefixed.
        key = key_b64
        if key.startswith("enc:"):
            key = key[4:]
        try:
            self._fernet = Fernet(key.encode("utf-8"))
        except (ValueError, TypeError):
            # Accept a bare urlsafe-b64 32-byte key and pad to Fernet format.
            raw = key.encode("ascii")
            try:
                raw = base64.urlsafe_b64decode(raw)
            except Exception:
                raise ValueError(
                    "ENCRYPTION_KEY is not valid base64. Generate with "
                    "'python scripts/generate_cipher_key.py'."
                )
            if len(raw) != 32:
                raise ValueError(
                    f"ENCRYPTION_KEY must decode to 32 bytes, got {len(raw)}. "
                    "Generate with 'python scripts/generate_cipher_key.py'."
                )
            import hashlib

            sign = base64.urlsafe_b64encode(hashlib.sha256(raw).digest())
            self._fernet = Fernet(base64.urlsafe_b64encode(raw) + sign)

    def encrypt(self, plaintext: str) -> str:
        """Encrypt a string → `enc:<base64>` (never returns plaintext)."""
        if not plaintext:
            return ""
        token = self._fernet.encrypt(plaintext.encode("utf-8"))
        return "enc:" + token.decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        """Decrypt `enc:<base64>` → original string.

        Raises:
            TokenDecryptionError: on bad key / tampered / corrupt data.
        """
        if not ciphertext:
            return ""
        if not ciphertext.startswith("enc:"):
            # Legacy / accidental plaintext — fail loudly, never return it.
            raise TokenDecryptionError("value is not encrypted")
        token = ciphertext[4:].encode("ascii")
        try:
            return self._fernet.decrypt(token).decode("utf-8")
        except (InvalidToken, ValueError) as exc:
            raise TokenDecryptionError(str(exc)) from exc


class TokenDecryptionError(Exception):
    """Raised when a stored token cannot be decrypted."""


_cipher: Cipher | None = None


def get_cipher() -> Cipher:
    global _cipher
    if _cipher is None:
        _cipher = Cipher(get_settings().encryption_key)
    return _cipher
