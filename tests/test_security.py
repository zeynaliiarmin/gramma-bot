"""Gramma — encryption unit tests (run: pytest tests/ -q)."""

from __future__ import annotations

import os
import sys

# Ensure the project root is importable when run directly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000:test")
os.environ.setdefault("ENCRYPTION_KEY", "9f6Bd2S19LFy/SQ/9Om4559N1Lgi5upd2zZGwvITWOA=")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./tests.db")

from app.core.config import Settings  # noqa: E402
from app.core.security.crypto import Cipher, TokenDecryptionError  # noqa: E402
from app.services.ai import classify_dm  # noqa: E402

settings = Settings()


def test_encrypt_decrypt_roundtrip():
    cipher = Cipher(settings.encryption_key)
    token = "EAAGm0PX4ZCwBA..." * 3
    ct = cipher.encrypt(token)
    assert ct != token
    assert ct.startswith("enc:")
    assert cipher.decrypt(ct) == token


def test_decrypt_plaintext_fails_loudly():
    cipher = Cipher(settings.encryption_key)
    with pytest.raises(TokenDecryptionError):
        cipher.decrypt("plaintext-token")


def test_tampered_ciphertext_rejected():
    cipher = Cipher(settings.encryption_key)
    ct = cipher.encrypt("secret")
    with pytest.raises(TokenDecryptionError):
        cipher.decrypt(ct[:-2] + "AA")


def test_different_keys_do_not_cross_decrypt():
    a = Cipher("9f6Bd2S19LFy/SQ/9Om4559N1Lgi5upd2zZGwvITWOA=")
    b = Cipher("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
    ct = a.encrypt("secret")
    with pytest.raises(Exception):
        b.decrypt(ct)


def test_classify_spam():
    label = classify_dm("برای فالوور رایگان کلیک کن", locale="fa")
    assert label == "spam"


def test_classify_normal():
    label = classify_dm("سلام میشه قیمت رو بگید؟", locale="fa")
    assert label == "needs_reply"
