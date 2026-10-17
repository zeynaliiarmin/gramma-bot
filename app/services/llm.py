"""Small, async text-LLM adapter: Gemini → OpenRouter :free → Mistral.

Only server-side environment keys are used. Nothing in this module publishes to
Instagram, Zernio or ChatbotX; callers decide what to do with generated text.
An absent/failed provider is skipped within one overall latency budget. Never
log request prompts, provider bodies, keys, or URL query tokens.
"""

from __future__ import annotations

import logging
import re
from time import monotonic

import httpx

from app.core.config import get_settings

logger = logging.getLogger("gramma.llm")


def configured() -> bool:
    return get_settings().llm_configured


async def generate_text(
    system: str, prompt: str, *, temperature: float = 0.7,
    timeout: float = 12.0, max_output_tokens: int = 650,
) -> str | None:
    """Return plain text or None; no retries of the same provider request.

    A single caller-supplied deadline spans *all* fallback providers. The
    priority order is stable and a model is only used when its key exists.
    """
    cfg = get_settings()
    if not isinstance(system, str) or not isinstance(prompt, str) or not prompt.strip():
        return None
    providers = [("gemini", cfg.gemini_api_key),
                 ("openrouter", cfg.openrouter_api_key),
                 ("mistral", cfg.mistral_api_key)]
    enabled = [(name, key) for name, key in providers if key]
    if not enabled:
        return None
    if not isinstance(timeout, (int, float)) or not 1 <= timeout <= 45:
        timeout = 12.0
    if not isinstance(temperature, (int, float)) or not 0 <= temperature <= 1:
        temperature = 0.7
    max_output_tokens = min(max(int(max_output_tokens), 32), 1200)
    system, prompt = system[:4000], prompt[:6000]
    deadline = monotonic() + timeout
    for index, (name, key) in enumerate(enabled):
        remaining = deadline - monotonic()
        if remaining < 0.5:
            break
        # A flaky first provider must not consume the entire webhook budget.
        per_provider = min(remaining, max(1.0, remaining / (len(enabled) - index)))
        try:
            if name == "gemini":
                model = cfg.gemini_model
                if (not re.fullmatch(r"gemini-[a-z0-9.-]{1,70}", model)
                        or "flash" not in model):
                    raise ValueError("invalid Gemini Flash model")
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
                headers = {"x-goog-api-key": key}
                body = {
                    "systemInstruction": {"parts": [{"text": system}]},
                    "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": temperature, "maxOutputTokens": max_output_tokens},
                }
            elif name == "openrouter":
                model = cfg.openrouter_model
                if not (re.fullmatch(r"[a-z0-9._-]+/[a-z0-9._-]+:free", model)
                        or model == "openrouter/free"):
                    raise ValueError("OpenRouter model must be a free variant")
                url = "https://openrouter.ai/api/v1/chat/completions"
                headers = {"Authorization": f"Bearer {key}"}
                body = {"model": model, "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ], "temperature": temperature, "max_tokens": max_output_tokens}
            else:
                model = cfg.mistral_model
                if not re.fullmatch(r"[a-z0-9._-]{1,80}", model):
                    raise ValueError("invalid Mistral model")
                url = "https://api.mistral.ai/v1/chat/completions"
                headers = {"Authorization": f"Bearer {key}"}
                body = {"model": model, "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ], "temperature": temperature, "max_tokens": max_output_tokens}
            async with httpx.AsyncClient(timeout=per_provider, follow_redirects=False) as client:
                response = await client.post(url, headers=headers, json=body)
            if response.status_code != 200:
                logger.warning("LLM provider=%s unavailable status=%d", name, response.status_code)
                continue
            data = response.json()
            if name == "gemini":
                parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
                result = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
            else:
                result = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                if isinstance(result, list):
                    result = "".join(p.get("text", "") for p in result if isinstance(p, dict))
            if isinstance(result, str) and result.strip():
                return result.strip()[:8000]
            logger.warning("LLM provider=%s returned no text", name)
        except Exception as exc:  # noqa: BLE001
            # The transport exception itself may include a URL/header.
            logger.warning("LLM provider=%s failed error=%s", name, type(exc).__name__)
    return None
