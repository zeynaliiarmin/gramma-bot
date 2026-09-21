"""
Gramma — application configuration.

All settings are read from environment variables / a `.env` file.
**No secrets are ever hard-coded in source code.**

Uses pydantic-settings for validation and sane defaults.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central configuration object (immutable once loaded)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── Telegram ──────────────────────────────────────────────
    telegram_bot_token: str = ""
    telegram_bot_username: str = ""
    admin_telegram_ids: str = ""  # comma separated numeric ids

    # ── Meta / Instagram Graph API ────────────────────────────
    instagram_account_mode: Literal["simulation", "production"] = "simulation"
    meta_app_id: str = ""
    meta_app_secret: str = ""
    meta_verify_token: str = "gramma-verify-token"

    webhook_base_url: str = ""
    webhook_path_prefix: str = "/webhook/instagram"
    graph_api_version: str = "v21.0"
    graph_api_base_url: str = "https://graph.facebook.com"
    instagram_api_base_url: str = "https://graph.instagram.com"

    # ── Database ──────────────────────────────────────────────
    database_url: str = "sqlite+aiosqlite:///./gramma.db"

    # ── Encryption ────────────────────────────────────────────
    encryption_key: str = ""

    # ── Redis / Celery ────────────────────────────────────────
    redis_url: str = "redis://localhost:6379/0"
    scheduler_backend: Literal["auto", "celery", "inprocess"] = "auto"
    post_scheduler_interval_seconds: int = 30
    daily_report_hour: int = 9
    timezone: str = "Asia/Tehran"
    auto_refresh_margin_minutes: int = 2880  # 48h safety margin

    # ── AI (AvalAI — production) / Tavily (web search) ────────
    avalai_api_key: str = ""
    avalai_base_url: str = "https://api.avalai.ir/v1"
    avalai_model: str = "gpt-4o-mini"
    # Legacy OpenAI-compatible knobs (kept for local swapping)
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    openai_base_url: str = ""
    tavily_api_key: str = ""

    # ── OpenClaw AI gateway (local HTTP, Telegram channel disabled) ──
    # OpenClaw runs on the same server as an HTTP gateway (DeepSeek/AvalAI).
    # Gramma is the ONLY client — and the ONLY owner of the Telegram token.
    # OPENCLAW_BASE_URL="" → feature disabled (template fallbacks used).
    openclaw_base_url: str = "http://127.0.0.1:18789"
    openclaw_token: str = ""
    openclaw_timeout: float = 30.0

    # ── Media CDN (optional) ─────────────────────────────────
    s3_bucket: str = ""
    s3_endpoint: str = ""
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_public_base: str = ""

    # ── Mini-App ──────────────────────────────────────────────
    # Public HTTPS URL of the Mini-App SPA (Vercel). Used when building the
    # Telegram WebApp button URL. Falls back to public_base_url.
    miniapp_public_url: str = ""

    # ── Misc ──────────────────────────────────────────────────
    log_level: str = "INFO"
    data_retention_days: int = 90
    # ── Platform limits (hard caps, single source of truth) ──
    # MAX_TOTAL_USERS: at most 24 Telegram users may register with the bot.
    # MAX_TOTAL_INSTAGRAM_ACCOUNTS: at most 24 Instagram pages in total.
    # MAX_ACCOUNTS_PER_USER: each user may connect at most 3 pages.
    # (enforced centrally in app/services/limits.py — bot AND Mini-App).
    max_total_users: int = 24
    max_total_instagram_accounts: int = Field(
        default=24,
        validation_alias=AliasChoices(
            "MAX_TOTAL_INSTAGRAM_ACCOUNTS", "MAX_TOTAL_ACCOUNTS_DEV"
        ),
    )
    max_accounts_per_user: int = 3
    # Control flags for the v3 superpowers
    auto_backup_enabled: bool = True
    auto_backup_hour: int = 4           # local time for the nightly DB backup
    semantic_search_enabled: bool = True
    public_base_url: str = ""           # public gateway for the Mini-App
    backup_dir: str = "backups"         # where rolling backups are written
    smart_notifications_enabled: bool = True
    # Reminder before a scheduled post fires (minutes).
    post_reminder_minutes_before: int = 30

    @property
    def admin_ids(self) -> set[int]:
        """Parse admin telegram ids into a set of ints."""
        out: set[int] = set()
        for part in self.admin_telegram_ids.replace(";", ",").split(","):
            part = part.strip()
            if part.isdigit():
                out.add(int(part))
        return out

    @property
    def miniapp_url(self) -> str:
        """Public Mini-App origin (dedicated var first, gateway fallback)."""
        return (self.miniapp_public_url or self.public_base_url or "").rstrip("/")

    @property
    def is_simulation(self) -> bool:
        """True when running without a real Meta connection."""
        return self.instagram_account_mode == "simulation"

    @property
    def ai_api_key(self) -> str:
        """Preferred AI key: AvalAI first, then legacy OpenAI."""
        return self.avalai_api_key or self.openai_api_key

    @property
    def ai_base_url(self) -> str:
        return self.avalai_base_url if self.avalai_api_key else (self.openai_base_url or None)

    @property
    def ai_model(self) -> str:
        return self.avalai_model or self.openai_model

    @property
    def graph_base(self) -> str:
        return f"{self.graph_api_base_url}/{self.graph_api_version}"

    @model_validator(mode="after")
    def _validate(self) -> "Settings":
        if not self.telegram_bot_token:
            raise ValueError(
                "TELEGRAM_BOT_TOKEN is required. Copy .env.example to .env "
                "and set a real token from @BotFather."
            )
        if not self.encryption_key:
            raise ValueError(
                "ENCRYPTION_KEY is required. Generate one with "
                "'python scripts/generate_cipher_key.py'."
            )
        if self.instagram_account_mode == "production":
            if not self.meta_app_id or not self.meta_app_secret:
                raise ValueError(
                    "META_APP_ID and META_APP_SECRET are required in production mode."
                )
            if not self.webhook_base_url:
                raise ValueError(
                    "WEBHOOK_BASE_URL is required in production mode "
                    "(must be a public HTTPS URL for the Meta webhook)."
                )
        return self


@lru_cache
def get_settings() -> Settings:
    """Return the cached, validated settings object."""
    return Settings()
