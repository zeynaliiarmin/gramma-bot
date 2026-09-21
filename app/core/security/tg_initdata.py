"""Gramma — Telegram WebApp initData validation.

Telegram signs every Mini-App launch with an `initData` string whose last
field is `hash=...` — an HMAC-SHA256 over the sorted data fields keyed with
`HMAC-SHA256(bot_token, "WebAppData")` (secret_key) — see the official
Telegram "Validating data received via the Mini App" guide. This module
implements *only* that canonical check: we never trust `user.id` before the
hash is verified.

We validate the same flags Telegram recommends:
    auth_date should be recent (max_age, default 2 days)
"""

from __future__ import annotations

import hmac
import hashlib
import json
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl, unquote


def _secret_key(bot_token: str) -> bytes:
    """HMAC-SHA256 key: HMAC("WebAppData", bot_token) per Telegram spec."""
    return hmac.new(b"WebAppData", bot_token.encode("ascii"), hashlib.sha256).digest()


def _raw_check_string(init_data: str) -> str:
    """Canonical check-string: sorted k=v pairs excluding the `hash` field."""
    data = {
        k: unquote(v)
        for k, v in parse_qsl(init_data, keep_blank_values=True)
        if k != "hash"
    }
    return "\n".join(f"{k}={data[k]}" for k in sorted(data))


def validate_init_data(init_data: str, bot_token: str, max_age: int = 2 * 86400) -> dict | None:
    """Return the decoded fields if `hash` is valid, else None.

    Follows the exact Telegram algorithm. A forged `hash` (or one signed with
    another bot's token) fails the HMAC comparison and returns None.
    """
    if not init_data or not bot_token:
        return None

    fields = dict(parse_qsl(init_data, keep_blank_values=True))
    if "hash" not in fields:
        return None

    provided = fields["hash"]
    check = _raw_check_string(init_data)
    computed = hmac.new(_secret_key(bot_token), check.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(computed, provided):
        return None

    # recommended freshness guard
    try:
        auth_date = int(fields.get("auth_date", "0"))
    except (TypeError, ValueError):
        auth_date = 0
    if auth_date and max_age and (int(time.time()) - auth_date) > max_age:
        return None

    # decode the nested JSON objects Telegram ships
    for key in ("user", "receiver", "chat"):
        raw = fields.get(key)
        if raw:
            try:
                fields[key] = json.loads(unquote(raw))
            except (ValueError, json.JSONDecodeError):
                fields[key] = raw
    return fields


@dataclass
class InitDataResult:
    """Convenience wrapper over a validated initData payload."""

    user_id: int
    username: str | None
    first_name: str
    fields: dict

    @classmethod
    def from_fields(cls, fields: dict) -> "InitDataResult | None":
        user = fields.get("user")
        if not isinstance(user, dict):
            return None
        try:
            user_id = int(user.get("id"))
        except (TypeError, ValueError):
            return None
        return cls(
            user_id=user_id,
            username=user.get("username"),
            first_name=user.get("first_name", ""),
            fields=fields,
        )
