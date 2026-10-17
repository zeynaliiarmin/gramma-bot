"""Bounded server-side text inference: Mistral → Gemini → live free OpenRouter.

Two ordered cycles are attempted within the caller's wall-clock deadline. The
OpenRouter catalogue is fetched when that tier is reached (and again on the
second cycle); no obsolete free model is pinned. API keys are read *only* from
os.getenv, never from source, prompts/HTTP bodies are never logged, and this
module cannot send a message to a follower.

Mistral Free mode has an account-level allowance and rate limits, NOT a promise
that an API key or a particular model is permanently free. The owner must keep
its plan/spend settings in Free mode. Gemini Flash free-tier availability also
depends on the owner's project and quotas. OpenRouter accepts only confirmed
zero-cost :free catalogue entries, including zero output/request pricing.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from decimal import Decimal, InvalidOperation
from time import monotonic

import httpx

logger = logging.getLogger("gramma.llm")

_MISTRAL_MODELS = ("mistral-small-latest", "ministral-14b-latest", "ministral-8b-latest")
_GEMINI_MODELS = ("gemini-3.8-flash", "gemini-2.5-flash")
_OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
_OPENROUTER_COMPLETIONS_URL = "https://openrouter.ai/api/v1/chat/completions"
_MISTRAL_COMPLETIONS_URL = "https://api.mistral.ai/v1/chat/completions"
_GEMINI_MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/models"
_FREE_MODEL_ID = re.compile(r"^[a-z0-9][a-z0-9._/-]{2,150}:free$", re.I)


class LLMUnavailable(RuntimeError):
    """No usable text from any enabled provider after two bounded cycles."""


def configured() -> bool:
    """Reflect live server environment, without caching key material."""
    return bool(os.getenv("MISTRAL_API_KEY") or os.getenv("GEMINI_API_KEY")
                or os.getenv("OPENROUTER_API_KEY"))


def _models(defaults: tuple[str, ...], env_name: str) -> tuple[str, ...]:
    """Honor an optional override only if it is a vetted free-tier candidate."""
    requested = os.getenv(env_name, "").strip()
    if requested and requested not in defaults:
        logger.warning("LLM model override %s ignored (not on free-tier shortlist)", env_name)
    return ((requested,) + tuple(m for m in defaults if m != requested)
            if requested in defaults else defaults)


def _zero(value: object) -> bool:
    if value is None or isinstance(value, bool):
        return False
    try:
        amount = Decimal(str(value))
        return amount.is_finite() and amount == 0
    except (InvalidOperation, ValueError):
        return False


def _free_openrouter_models(catalogue: object, max_output_tokens: int) -> list[dict]:
    """Preserve OpenRouter's live most-popular ordering among safe text models.

    The provider's current popularity ordering is a transparent quality/use
    proxy, not a claim of an objectively best model. Require :free and zero
    prompt, completion and per-request charges, with no costly overrides.
    """
    if not isinstance(catalogue, dict) or not isinstance(catalogue.get("data"), list):
        return []
    if (isinstance(catalogue.get("links"), dict)
            and catalogue["links"].get("next")):
        # We request the unpaginated list; an incomplete catalogue cannot
        # prove that the chosen model is the best eligible one.
        return []
    eligible: list[dict] = []
    for item in catalogue["data"]:
        if not isinstance(item, dict):
            continue
        name = item.get("id")
        if not isinstance(name, str) or not _FREE_MODEL_ID.fullmatch(name) or "/" not in name:
            continue
        prices = item.get("pricing")
        if not isinstance(prices, dict) or not (_zero(prices.get("prompt"))
                and _zero(prices.get("completion"))):
            continue
        if "request" in prices and not _zero(prices["request"]):
            continue
        if "internal_reasoning" in prices and not _zero(prices["internal_reasoning"]):
            continue
        overrides = prices.get("overrides") or []
        if not isinstance(overrides, list) or any(
            not isinstance(override, dict) or any(
                key in override and not _zero(override[key])
                for key in ("prompt", "completion", "request", "internal_reasoning")
            ) for override in overrides
        ):
            continue
        architecture = item.get("architecture") or {}
        if not isinstance(architecture, dict):
            continue
        if ("text" not in (architecture.get("input_modalities") or [])
                or "text" not in (architecture.get("output_modalities") or [])):
            continue
        context_length = item.get("context_length")
        if not isinstance(context_length, (int, float)) or context_length < 8192:
            continue
        top_provider = item.get("top_provider") or {}
        if not isinstance(top_provider, dict):
            continue
        ceiling = top_provider.get("max_completion_tokens")
        if isinstance(ceiling, (int, float)) and ceiling < 32:
            continue
        supports = item.get("supported_parameters") or []
        if not isinstance(supports, list) or "max_tokens" not in supports:
            # Never request a model that cannot bound its output budget.
            continue
        # Keep requests compatible with advertised generation controls.
        eligible.append({
            "id": name,
            "temperature": "temperature" in supports,
            "max_tokens": "max_tokens" in supports,
            "ceiling": int(ceiling) if isinstance(ceiling, (int, float)) else max_output_tokens,
        })
        if len(eligible) == 2:  # next free model if the most popular fails
            break
    return eligible


def _next_timeout(deadline: float, remaining_operations: int) -> float:
    """Reserve a fair chance for each later provider and the second cycle."""
    remaining = deadline - monotonic()
    if remaining < 0.65:
        return 0.0
    return min(6.0, remaining, max(0.65, remaining / max(1, remaining_operations)))


async def _request(client: httpx.AsyncClient, method: str, url: str, *,
                   seconds: float, **kwargs) -> httpx.Response:
    # httpx timeouts can apply independently to connect/read/write; wait_for
    # also bounds the *whole* operation against the shared cron deadline.
    return await asyncio.wait_for(client.request(method, url, **kwargs), timeout=seconds)


def _extract_text(provider: str, data: object) -> str | None:
    if not isinstance(data, dict):
        return None
    choices = data.get("candidates" if provider == "gemini" else "choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None
    first = choices[0]
    if provider == "gemini":
        content = first.get("content") or {}
        parts = content.get("parts") if isinstance(content, dict) else None
        result = "".join(p.get("text", "") for p in parts
                         if isinstance(p, dict) and isinstance(p.get("text"), str)) if isinstance(parts, list) else ""
    else:
        message = first.get("message") or {}
        result = message.get("content") if isinstance(message, dict) else None
        if isinstance(result, list):
            result = "".join(p.get("text", "") for p in result
                             if isinstance(p, dict) and isinstance(p.get("text"), str))
    return result.strip()[:8000] if isinstance(result, str) and result.strip() else None


async def generate_text(
    system: str, prompt: str, *, temperature: float = 0.7,
    timeout: float = 12.0, max_output_tokens: int = 650,
    raise_on_unavailable: bool = False,
) -> str | None:
    """Produce text or fail closed; two Mistral → Gemini → OpenRouter cycles.

    Non-worker callers keep the historical None-on-failure API. Durable inbox
    callers request LLMUnavailable so a full outage requeues instead of
    incorrectly marking their inbound message terminal. No LLM call itself
    sends a reply; a separate quota-reserving sender controls delivery.
    """
    if not isinstance(system, str) or not isinstance(prompt, str) or not prompt.strip():
        return None
    keys = {
        "mistral": os.getenv("MISTRAL_API_KEY", "").strip(),
        "gemini": os.getenv("GEMINI_API_KEY", "").strip(),
        "openrouter": os.getenv("OPENROUTER_API_KEY", "").strip(),
    }
    enabled = tuple(name for name in ("mistral", "gemini", "openrouter") if keys[name])
    if not enabled:
        if raise_on_unavailable:
            raise LLMUnavailable("no_provider_configured")
        return None
    if not isinstance(timeout, (int, float)) or not 1 <= timeout <= 120:
        timeout = 12.0
    if not isinstance(temperature, (int, float)) or not 0 <= temperature <= 1:
        temperature = 0.7
    max_output_tokens = min(max(int(max_output_tokens), 32), 1200)
    system, prompt = system[:4000], prompt[:6000]
    mistral = _models(_MISTRAL_MODELS, "MISTRAL_MODEL")
    gemini = _models(_GEMINI_MODELS, "GEMINI_MODEL")
    # Two possible model POSTs plus one live catalogue GET at OpenRouter.
    operations_per_cycle = (len(mistral) if keys["mistral"] else 0) + (
        len(gemini) if keys["gemini"] else 0
    ) + (3 if keys["openrouter"] else 0)
    operations_left = 2 * operations_per_cycle
    deadline = monotonic() + timeout
    async with httpx.AsyncClient(timeout=12.0, follow_redirects=False) as client:
        for cycle in range(2):
            for provider in enabled:
                if provider == "mistral":
                    choices: list[dict] = [{"id": model} for model in mistral]
                elif provider == "gemini":
                    choices = [{"id": model} for model in gemini]
                else:
                    seconds = _next_timeout(deadline, operations_left)
                    if not seconds:
                        break
                    try:
                        response = await _request(
                            client, "GET", _OPENROUTER_MODELS_URL, seconds=seconds,
                            headers={"Authorization": f"Bearer {keys[provider]}"},
                            params={"input_modalities": "text", "output_modalities": "text",
                                    "max_price": 0, "sort": "most-popular"},
                        )
                        if response.status_code != 200:
                            logger.warning("LLM provider=openrouter catalogue_status=%d cycle=%d",
                                           response.status_code, cycle + 1)
                            choices = []
                        else:
                            choices = _free_openrouter_models(response.json(), max_output_tokens)
                            if not choices:
                                logger.warning("LLM provider=openrouter no eligible free text models")
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("LLM provider=openrouter catalogue_error=%s",
                                       type(exc).__name__)
                        choices = []
                    operations_left -= 1
                    operations_left -= 2 - len(choices)
                for model in choices:
                    seconds = _next_timeout(deadline, operations_left)
                    if not seconds:
                        break
                    model_id = model["id"]
                    if provider == "gemini":
                        url = f"{_GEMINI_MODELS_URL}/{model_id}:generateContent"
                        headers = {"x-goog-api-key": keys[provider]}
                        body = {
                            "systemInstruction": {"parts": [{"text": system}]},
                            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                            "generationConfig": {"temperature": temperature,
                                                 "maxOutputTokens": max_output_tokens},
                        }
                    else:
                        url = (_MISTRAL_COMPLETIONS_URL if provider == "mistral"
                               else _OPENROUTER_COMPLETIONS_URL)
                        headers = {"Authorization": f"Bearer {keys[provider]}"}
                        body = {"model": model_id, "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": prompt},
                        ]}
                        if provider == "mistral":
                            body.update(temperature=temperature, max_tokens=max_output_tokens)
                        else:
                            if model["temperature"]:
                                body["temperature"] = temperature
                            if model["max_tokens"]:
                                body["max_tokens"] = min(max_output_tokens, model["ceiling"])
                    try:
                        response = await _request(client, "POST", url, seconds=seconds,
                                                  headers=headers, json=body)
                        if response.status_code != 200:
                            logger.warning("LLM provider=%s model=%s status=%d cycle=%d",
                                           provider, model_id, response.status_code, cycle + 1)
                        else:
                            result = _extract_text(provider, response.json())
                            if result:
                                return result
                            logger.warning("LLM provider=%s model=%s returned no text", provider,
                                           model_id)
                    except Exception as exc:  # noqa: BLE001
                        # Never log exception details: URLs/headers can contain secrets.
                        logger.warning("LLM provider=%s model=%s error=%s cycle=%d",
                                       provider, model_id, type(exc).__name__, cycle + 1)
                    finally:
                        operations_left -= 1
            if deadline - monotonic() < 0.65:
                break
    if raise_on_unavailable:
        raise LLMUnavailable("all_providers_unavailable")
    return None
