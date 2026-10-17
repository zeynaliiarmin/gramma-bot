"""Two bounded LLM cycles with *explicitly zero-priced* live model selection.

Order: Mistral, Gemini, OpenRouter. Mistral/Google model listings describe
availability/capabilities but (currently) do NOT attest free-tier pricing.
A model with absent/ambiguous input or output price is never sent a request,
including models previously hard-coded in this app. If a provider later adds
unambiguous zero pricing to its model metadata it becomes eligible automatically.
OpenRouter's live :free catalogue currently supplies explicit prices.

Keys come only from os.getenv. No model inference is called during import,
builds or health checks. Provider responses/prompts/API keys are never logged.
An HTTP 402/403 blocks that provider+model for 24h in private Supabase state.
This module cannot send an Instagram or Telegram message.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from time import monotonic

import httpx
from sqlalchemy import select

logger = logging.getLogger("gramma.llm")

_MISTRAL_MODELS_URL = "https://api.mistral.ai/v1/models"
_MISTRAL_COMPLETIONS_URL = "https://api.mistral.ai/v1/chat/completions"
_GEMINI_MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/models"
_OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
_OPENROUTER_COMPLETIONS_URL = "https://openrouter.ai/api/v1/chat/completions"
_FREE_MODEL_ID = re.compile(r"^[a-z0-9][a-z0-9._/-]{2,150}:free$", re.I)
_SAFE_MODEL_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{2,145}$", re.I)
_VERSION = re.compile(r"(?<!\d)(\d{1,2})(?:\.(\d{1,2}))?")
_DISCOVERY_TTL = timedelta(hours=6)  # discover a newly free model within a workday
_FAILURE_TTL = timedelta(minutes=30)


class LLMUnavailable(RuntimeError):
    """No verified zero-priced text model responded after two cycles."""


def configured() -> bool:
    """The presence of a key is not evidence a free model is available."""
    return bool(os.getenv("MISTRAL_API_KEY") or os.getenv("GEMINI_API_KEY")
                or os.getenv("OPENROUTER_API_KEY"))


def _zero(value: object) -> bool:
    if value is None or isinstance(value, bool):
        return False
    try:
        amount = Decimal(str(value))
        return amount.is_finite() and amount == 0
    except (InvalidOperation, ValueError):
        return False


def _explicit_zero_price(item: dict, *, require_request: bool = False) -> bool:
    """Fail closed when any applicable price lacks an explicit numeric zero."""
    prices = item.get("pricing")
    if not isinstance(prices, dict):
        return False
    prompt = prices.get("prompt", prices.get("input"))
    completion = prices.get("completion", prices.get("output"))
    if not (_zero(prompt) and _zero(completion)):
        return False
    if require_request and not _zero(prices.get("request")):
        return False
    for key in ("request", "internal_reasoning", "per_request", "input_cache_read", "input_cache_write"):
        if key in prices and not _zero(prices[key]):
            return False
    overrides = prices.get("overrides") or []
    if not isinstance(overrides, list):
        return False
    for override in overrides:
        if not isinstance(override, dict):
            return False
        if not (_zero(override.get("prompt", override.get("input")))
                and _zero(override.get("completion", override.get("output")))):
            return False
        for key in ("request", "internal_reasoning", "per_request", "input_cache_read", "input_cache_write"):
            if key in override and not _zero(override[key]):
                return False
    return True


def _deprecated(item: dict) -> bool:
    if item.get("archived") is True or item.get("deprecated") is True or item.get("is_deprecated") is True:
        return True
    if str(item.get("state") or item.get("status") or "").lower() in (
        "deprecated", "retired", "shutdown", "unavailable", "archived"
    ):
        return True
    date_text = (item.get("deprecation_date") or item.get("deprecated_at")
                 or item.get("shutdown_at") or item.get("expiration_date"))
    if isinstance(date_text, str):
        try:
            return datetime.fromisoformat(date_text.replace("Z", "+00:00")) <= datetime.now(timezone.utc)
        except ValueError:
            return True  # malformed deprecation information is unsafe
    return False


def _quality_key(item: dict) -> tuple:
    """Transparent release/capability heuristic, NOT an objective quality score."""
    name = item["id"]
    matches = list(_VERSION.finditer(name))
    version = (int(matches[-1].group(1)), int(matches[-1].group(2) or 0)) if matches else (0, 0)
    created = item.get("created")
    try:
        created_number = int(created)
    except (TypeError, ValueError):
        created_number = 0
    return (version[0], version[1], "lite" not in name,
            "14b" in name or "small" in name, created_number,
            min(int(item.get("context", 0)), 1_000_000))


def _free_provider_models(provider: str, catalogue: object) -> list[dict]:
    if not isinstance(catalogue, dict):
        return []
    entries = catalogue.get("data" if provider == "mistral" else "models")
    if not isinstance(entries, list) or (provider == "gemini" and catalogue.get("nextPageToken")):
        # An incomplete Google catalogue cannot safely deprecate old models.
        return []
    models = []
    for item in entries:
        if not isinstance(item, dict) or _deprecated(item) or not _explicit_zero_price(item):
            continue
        name = item.get("id") if provider == "mistral" else item.get("name")
        if provider == "gemini" and isinstance(name, str) and name.startswith("models/"):
            name = name.removeprefix("models/")
        if not isinstance(name, str) or not _SAFE_MODEL_ID.fullmatch(name):
            continue
        label = " ".join((name, str(item.get("displayName") or ""), str(item.get("description") or ""))).lower()
        if not any(term in label for term in ("free", "flash", "small", "ministral")):
            continue
        if any(term in name.lower() for term in ("embedding", "tts", "audio", "image", "vision", "realtime", "live", "preview", "experimental")):
            continue
        if provider == "mistral":
            capabilities = item.get("capabilities")
            if not isinstance(capabilities, dict) or capabilities.get("completion_chat") is not True:
                continue
            context = item.get("max_context_length")
        else:
            if "generateContent" not in (item.get("supportedGenerationMethods") or []):
                continue
            context = item.get("inputTokenLimit")
        if not isinstance(context, int) or context < 8192:
            continue
        models.append({"id": name, "context": context,
                       "created": item.get("created") if provider == "mistral" else 0})
    models.sort(key=_quality_key, reverse=True)
    return models[:4]


def _free_openrouter_models(catalogue: object, max_output_tokens: int) -> list[dict]:
    """Provider's live most-popular order among text models with zero pricing."""
    if not isinstance(catalogue, dict) or not isinstance(catalogue.get("data"), list):
        return []
    if isinstance(catalogue.get("links"), dict) and catalogue["links"].get("next"):
        return []
    eligible = []
    for item in catalogue["data"]:
        if not isinstance(item, dict) or _deprecated(item):
            continue
        name = item.get("id")
        if not isinstance(name, str) or not _FREE_MODEL_ID.fullmatch(name) or "/" not in name:
            continue
        if not _explicit_zero_price(item, require_request=True):
            continue
        architecture = item.get("architecture")
        if not isinstance(architecture, dict) or ("text" not in (architecture.get("input_modalities") or [])
                                                     or "text" not in (architecture.get("output_modalities") or [])):
            continue
        context = item.get("context_length")
        if not isinstance(context, int) or context < 8192:
            continue
        top_provider = item.get("top_provider") or {}
        if not isinstance(top_provider, dict):
            continue
        ceiling = top_provider.get("max_completion_tokens")
        if isinstance(ceiling, (int, float)) and ceiling < 32:
            continue
        supports = item.get("supported_parameters") or []
        if not isinstance(supports, list) or "max_tokens" not in supports:
            continue
        eligible.append({"id": name, "temperature": "temperature" in supports,
                         "ceiling": min(int(ceiling), 1200) if isinstance(ceiling, (int, float)) else max_output_tokens})
        if len(eligible) == 4:
            break
    return eligible


def _next_timeout(deadline: float, operations: int) -> float:
    remaining = deadline - monotonic()
    if remaining < 0.65:
        return 0.0
    return min(7.0, remaining, max(0.65, remaining / max(1, operations)))


async def _request(client: httpx.AsyncClient, method: str, url: str, *, seconds: float, **kwargs) -> httpx.Response:
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


async def _active_blocks() -> set[tuple[str, str]]:
    from app.core.database import SessionLocal
    from app.models.knowledge import LLMModelBlacklist

    async with SessionLocal() as session:
        rows = (await session.execute(select(LLMModelBlacklist).where(
            LLMModelBlacklist.blocked_until > datetime.now(timezone.utc)
        ))).scalars().all()
    return {(row.provider, row.model_id) for row in rows}


async def _block_model(provider: str, model_id: str, status: int) -> None:
    from app.core.database import SessionLocal
    from app.models.knowledge import LLMModelBlacklist

    now = datetime.now(timezone.utc)
    async with SessionLocal() as session:
        row = await session.get(LLMModelBlacklist, (provider, model_id))
        if row is None:
            row = LLMModelBlacklist(provider=provider, model_id=model_id,
                                    reason=f"http_{status}", blocked_until=now + timedelta(hours=24))
            session.add(row)
        else:
            row.reason, row.blocked_until = f"http_{status}", now + timedelta(hours=24)
        await session.commit()


async def _cached_provider_models(
    provider: str, key: str, client: httpx.AsyncClient, deadline: float,
) -> list[dict]:
    """Daily cached GET from Mistral/Google; no eligible price => no POST."""
    from app.core.database import SessionLocal
    from app.models.knowledge import LLMModelCatalogue

    async with SessionLocal() as session:
        cached = await session.get(LLMModelCatalogue, provider)
        if cached is not None:
            checked = cached.checked_at
            if checked.tzinfo is None:
                checked = checked.replace(tzinfo=timezone.utc)
            age = datetime.now(timezone.utc) - checked
            ttl = _FAILURE_TTL if cached.status == "lookup_failed" else _DISCOVERY_TTL
            # Never POST using yesterday's price: a positive catalogue gets
            # revalidated by GET before each generation. Cache negative finds.
            if cached.status != "eligible" and timedelta(0) <= age < ttl:
                return []
    seconds = _next_timeout(deadline, 9)
    if not seconds:
        return []
    url = _MISTRAL_MODELS_URL if provider == "mistral" else _GEMINI_MODELS_URL
    headers = ({"Authorization": f"Bearer {key}"} if provider == "mistral"
               else {"x-goog-api-key": key})
    params = {} if provider == "mistral" else {"pageSize": 1000}
    try:
        response = await _request(client, "GET", url, seconds=seconds, headers=headers, params=params)
        if response.status_code == 200:
            models = _free_provider_models(provider, response.json())
            status = "eligible" if models else "no_verified_free"
        else:
            logger.warning("LLM model discovery provider=%s status=%d", provider, response.status_code)
            models, status = [], "lookup_failed"
    except Exception as exc:  # noqa: BLE001
        logger.warning("LLM model discovery provider=%s error=%s", provider, type(exc).__name__)
        models, status = [], "lookup_failed"
    # Persist only safe model IDs/status; never store pricing payloads or keys.
    async with SessionLocal() as session:
        row = await session.get(LLMModelCatalogue, provider)
        if row is None:
            row = LLMModelCatalogue(provider=provider, models=models, status=status)
            session.add(row)
        else:
            row.models, row.status, row.checked_at = models, status, datetime.now(timezone.utc)
        try:
            await session.commit()
        except Exception:  # noqa: BLE001
            # A concurrent request may have inserted the same provider row.
            await session.rollback()
            logger.warning("LLM catalogue cache write unavailable provider=%s", provider)
    return models


async def generate_text(
    system: str, prompt: str, *, temperature: float = 0.7, timeout: float = 12.0,
    max_output_tokens: int = 650, raise_on_unavailable: bool = False,
) -> str | None:
    """Try two Mistral→Gemini→OpenRouter cycles; only verified free models.

    No model gets a generation request solely because its name says 'free'.
    Unavailable text returns None to draft-only callers; the durable inbox
    caller raises LLMUnavailable and schedules its message for next-day hold.
    """
    if not isinstance(system, str) or not isinstance(prompt, str) or not prompt.strip():
        return None
    keys = {"mistral": os.getenv("MISTRAL_API_KEY", "").strip(),
            "gemini": os.getenv("GEMINI_API_KEY", "").strip(),
            "openrouter": os.getenv("OPENROUTER_API_KEY", "").strip()}
    enabled = [provider for provider in ("mistral", "gemini", "openrouter") if keys[provider]]
    if not enabled:
        if raise_on_unavailable:
            raise LLMUnavailable("no_provider_configured")
        return None
    if not isinstance(timeout, (int, float)) or not 1 <= timeout <= 120:
        timeout = 12.0
    if not isinstance(temperature, (int, float)) or not 0 <= temperature <= 1:
        temperature = 0.7
    max_output_tokens = min(max(int(max_output_tokens), 32), 1200)
    system, prompt = system[:4600], prompt[:6000]
    deadline = monotonic() + timeout
    try:
        blocked = await _active_blocks()
    except Exception as exc:  # noqa: BLE001
        logger.warning("LLM private model blacklist unavailable: %s", type(exc).__name__)
        if raise_on_unavailable:
            raise LLMUnavailable("blacklist_unavailable") from None
        return None
    discovery: dict[str, list[dict]] = {}
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
        for cycle in range(2):
            for provider in enabled:
                if deadline - monotonic() < 0.65:
                    break
                if provider in ("mistral", "gemini"):
                    if provider not in discovery:
                        try:
                            discovery[provider] = await _cached_provider_models(
                                provider, keys[provider], client, deadline)
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("LLM discovery unavailable provider=%s error=%s",
                                           provider, type(exc).__name__)
                            discovery[provider] = []
                    choices = discovery[provider]
                else:
                    seconds = _next_timeout(deadline, 9)
                    if not seconds:
                        continue
                    try:
                        response = await _request(client, "GET", _OPENROUTER_MODELS_URL,
                                                  seconds=seconds,
                                                  headers={"Authorization": f"Bearer {keys[provider]}"},
                                                  params={"input_modalities": "text",
                                                          "output_modalities": "text",
                                                          "max_price": 0, "sort": "most-popular"})
                        if response.status_code == 200:
                            choices = _free_openrouter_models(response.json(), max_output_tokens)
                        else:
                            logger.warning("LLM OpenRouter catalogue status=%d", response.status_code)
                            choices = []
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("LLM OpenRouter catalogue unavailable error=%s", type(exc).__name__)
                        choices = []
                for model in choices:
                    model_id = model.get("id") if isinstance(model, dict) else None
                    if not isinstance(model_id, str) or (provider, model_id) in blocked:
                        continue
                    seconds = _next_timeout(deadline, max(2, 8 - cycle * 3))
                    if not seconds:
                        break
                    if provider == "gemini":
                        url = f"{_GEMINI_MODELS_URL}/{model_id}:generateContent"
                        headers = {"x-goog-api-key": keys[provider]}
                        body = {"systemInstruction": {"parts": [{"text": system}]},
                                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                                "generationConfig": {"temperature": temperature,
                                                     "maxOutputTokens": max_output_tokens}}
                    else:
                        url = (_MISTRAL_COMPLETIONS_URL if provider == "mistral"
                               else _OPENROUTER_COMPLETIONS_URL)
                        headers = {"Authorization": f"Bearer {keys[provider]}"}
                        body = {"model": model_id,
                                "messages": [{"role": "system", "content": system},
                                             {"role": "user", "content": prompt}]}
                        if provider == "mistral":
                            body.update(temperature=temperature, max_tokens=max_output_tokens)
                        else:
                            if model.get("temperature"):
                                body["temperature"] = temperature
                            body["max_tokens"] = min(max_output_tokens, int(model["ceiling"]))
                    try:
                        response = await _request(client, "POST", url, seconds=seconds,
                                                  headers=headers, json=body)
                        if response.status_code in (402, 403):
                            blocked.add((provider, model_id))
                            try:
                                await _block_model(provider, model_id, response.status_code)
                            except Exception as exc:  # noqa: BLE001
                                # No persistent blacklist => stop this invocation.
                                logger.warning("LLM blacklist write failed: %s", type(exc).__name__)
                                if raise_on_unavailable:
                                    raise LLMUnavailable("blacklist_write_failed") from None
                                return None
                        elif response.status_code != 200:
                            logger.warning("LLM provider=%s model=%s status=%d cycle=%d",
                                           provider, model_id, response.status_code, cycle + 1)
                        else:
                            result = _extract_text(provider, response.json())
                            if result:
                                return result
                            logger.warning("LLM provider=%s model=%s returned no text", provider, model_id)
                    except LLMUnavailable:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("LLM provider=%s model=%s error=%s cycle=%d",
                                       provider, model_id, type(exc).__name__, cycle + 1)
    if raise_on_unavailable:
        raise LLMUnavailable("all_providers_unavailable")
    return None
