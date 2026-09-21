"""Gramma — Instagram Graph API client (thin, defensive HTTP wrapper).

All calls go through here so error handling and rate-limit backoff live in
one place. Every method is a coroutine that returns parsed JSON or raises
a typed `GraphAPIError`.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from app.core.config import get_settings

settings = get_settings()
_BASE = f"{settings.graph_api_base_url}/{settings.graph_api_version}"


class GraphAPIError(Exception):
    """Typed error from the Instagram Graph API."""

    def __init__(self, code: int | None, type_: str, message: str, status: int = 0):
        self.code = code
        self.type = type_
        self.message = message
        self.status = status
        super().__init__(f"[{status}] {type_}({code}): {message}")

    @property
    def is_auth_error(self) -> bool:
        return (
            self.code in (190, 100, 102, 104)
            or "expired" in str(self.message).lower()
            or "Invalid OAuth" in str(self.message)
        )


class GraphClient:
    """Async client with automatic retry/backoff for 5xx + rate limits."""

    def __init__(self, timeout: float = 30.0) -> None:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout), follow_redirects=True
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def request(
        self,
        method: str,
        path: str,
        token: str | None = None,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        retries: int = 3,
    ) -> dict[str, Any]:
        url = f"{_BASE}/{path.lstrip('/')}"
        if token:
            params = dict(params or {})
            params["access_token"] = token

        for attempt in range(retries):
            try:
                resp = await self._client.request(method, url, params=params, json=json)
            except httpx.HTTPError as exc:  # network blip → retry
                if attempt == retries - 1:
                    raise GraphAPIError(None, "network", str(exc)) from exc
                await asyncio.sleep(2**attempt)
                continue

            body = {}
            try:
                body = resp.json()
            except Exception:
                body = {"error": {"message": resp.text[:500]}}
            body = body or {}

            if resp.status_code in (429, 500, 502, 503, 504):
                if attempt < retries - 1:
                    await asyncio.sleep(2 ** (attempt + 1))
                    continue

            if "error" in body and body["error"]:
                err = body["error"]
                raise GraphAPIError(
                    code=err.get("code"),
                    type_=err.get("type") or err.get("error_subcode") or "graph_error",
                    message=err.get("message", "unknown error"),
                    status=resp.status_code,
                )

            return body

        raise GraphAPIError(None, "retries_exhausted", f"GET {path}")

    # ── Convenience wrappers ─────────────────────────────────
    async def get(self, path: str, token: str, **params) -> dict[str, Any]:
        return await self.request("GET", path, token=token, params=params)

    async def post(self, path: str, token: str, **params) -> dict[str, Any]:
        return await self.request("POST", path, token=token, params=params)


_graph_client: GraphClient | None = None


def get_client() -> GraphClient:
    global _graph_client
    if _graph_client is None:
        _graph_client = GraphClient()
    return _graph_client
