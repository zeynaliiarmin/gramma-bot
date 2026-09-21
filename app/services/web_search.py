"""Gramma v3 — Tavily web search + AvalAI caption engine.

`web_search` lets users research ideas (الهام محتوایی) right inside the bot,
and `generate_caption` (app/services/ai.py) now uses AvalAI in production.
"""

from __future__ import annotations

import logging

import httpx

from app.core.config import get_settings

settings = get_settings()
logger = logging.getLogger("gramma.search_web")

_TAVILY_URL = "https://api.tavily.com/search"


async def web_search(query: str, max_results: int = 5) -> list[dict]:
    """Run a Tavily web search; returns [{title, url, snippet}]."""
    if not settings.tavily_api_key:
        return []
    payload = {
        "api_key": settings.tavily_api_key,
        "query": query,
        "max_results": max_results,
        "search_depth": "basic",
    }
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(_TAVILY_URL, json=payload)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("tavily search failed: %s", exc)
        return []

    results = []
    for item in data.get("results", []):
        results.append(
            {
                "title": item.get("title", "")[:120],
                "url": item.get("url", ""),
                "snippet": (item.get("content") or "")[:220],
            }
        )
    return results


def format_web_results(query: str, results: list[dict]) -> str:
    if not results:
        return "🔍 نتیجه‌ای از جستجوی وب یافت نشد (یا سرویس در دسترس نیست)."
    lines = [f"🌐 نتایج جستجوی وب برای «{query}»:\n"]
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. <a href='{r['url']}'>{r['title']}</a>\n   {r['snippet']}")
    return "\n".join(lines)
