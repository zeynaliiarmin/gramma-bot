"""Gramma — Meta OAuth 2.0 helpers (PKCE + Instagram Login).

Implements the authorization flow documented at
https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets

from app.core.config import get_settings

settings = get_settings()

# Scopes for a Business/Creator IG account with content publishing + DMs.
OAUTH_SCOPES = [
    "instagram_business_basic",
    "instagram_business_content_publish",
    "instagram_business_manage_messages",
    "instagram_business_manage_comments",
    "instagram_business_manage_insights",
    "business_management",
    "pages_show_list",
    "pages_read_engagement",
    "pages_manage_metadata",
]


def generate_pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for PKCE (S256)."""
    code_verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return code_verifier, code_challenge


def build_authorization_url(redirect_uri: str, state: str) -> str:
    """Build the Meta OAuth dialog URL (with PKCE S256 + state)."""
    verifier, challenge = generate_pkce_pair()
    params = {
        "client_id": settings.meta_app_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "response_type": "code",
        "scope": ",".join(OAUTH_SCOPES),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "config_id": "",  # optional login configuration
    }
    query = "&".join(f"{k}={quote(str(v))}" for k, v in params.items() if v)
    return f"{settings.graph_api_base_url}/oauth/authorize?{query}", verifier


def quote(value: str) -> str:
    from urllib.parse import quote as _quote

    return _quote(value, safe="")


async def fetch_app_token() -> str:
    """Fetch a Meta app access token (server-to-server, used by the webhook)."""
    import httpx

    resp = await httpx.AsyncClient().get(
        f"{settings.graph_api_base_url}/oauth/access_token",
        params={
            "client_id": settings.meta_app_id,
            "client_secret": settings.meta_app_secret,
            "grant_type": "client_credentials",
        },
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


async def exchange_code_for_token(code: str, redirect_uri: str) -> dict:
    """Exchange a short-lived authorization code for a short-lived token."""
    import httpx

    resp = await httpx.AsyncClient().get(
        f"{settings.graph_api_base_url}/oauth/access_token",
        params={
            "client_id": settings.meta_app_id,
            "client_secret": settings.meta_app_secret,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
            "code": code,
        },
    )
    resp.raise_for_status()
    data = resp.json()
    if "access_token" not in data:
        raise RuntimeError(f"token exchange failed: {data}")
    return data


async def exchange_long_lived_token(short_lived_token: str) -> dict:
    """Swap a short-lived token for a long-lived one (≈60 days)."""
    import httpx

    resp = await httpx.AsyncClient().get(
        f"{settings.graph_api_base_url}/oauth/access_token",
        params={
            "grant_type": "fb_exchange_token",
            "client_id": settings.meta_app_id,
            "client_secret": settings.meta_app_secret,
            "fb_exchange_token": short_lived_token,
        },
    )
    resp.raise_for_status()
    return resp.json()


def decrypt_app_secret() -> str:
    """Return the raw Meta app secret (supports `enc:` encrypted value)."""
    from app.core.security.crypto import get_cipher

    secret = settings.meta_app_secret
    if secret.startswith("enc:"):
        return get_cipher().decrypt(secret)
    return secret
