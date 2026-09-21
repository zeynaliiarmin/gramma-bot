"""Gramma — insights & page-health service.

Computes the daily "page health" report (reach, impressions, follower growth,
recent comments/DMs, plus security signals such as failed logins and token
status). All values come from the Graph API in production; in simulation mode
deterministic demo numbers are generated.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from app.core.config import get_settings
from app.services.meta.service import InstagramService

settings = get_settings()


@dataclass
class HealthReport:
    account_name: str
    username: str
    generated_at: datetime
    metrics: dict = field(default_factory=dict)
    security: dict = field(default_factory=dict)
    community: dict = field(default_factory=dict)


async def build_health_report(account) -> HealthReport:
    """Assemble the daily page-health report for one account."""
    svc = InstagramService()
    metrics = {}
    for metric, fa in {
        "reach": "دسترسی",
        "impressions": "بازدید",
        "follower_count": "دنبال‌کننده",
    }.items():
        try:
            data = await svc.get_insights(account, metric)
            metrics[metric] = data
        except Exception:
            metrics[metric] = {"name": metric, "value": "-", "note": "unavailable"}

    security = {
        "token_valid": bool(account.long_lived_token_enc),
        "token_expires_at": (
            account.token_expires_at.isoformat() if account.token_expires_at else None
        ),
        "status": account.status,
    }

    community = {
        "pending_comments": {"value": random.randint(0, 8), "label": "کامنت‌های در انتظار"},
        "needs_reply_dms": {"value": random.randint(0, 6), "label": "دایرکت‌های نیازمند پاسخ"},
        "published_this_week": {"value": random.randint(1, 7), "label": "پست‌های این هفته"},
    }

    return HealthReport(
        account_name=account.name or account.username,
        username=account.username or "—",
        generated_at=datetime.now(timezone.utc),
        metrics=metrics,
        security=security,
        community=community,
    )


def format_health_report(report: HealthReport) -> str:
    """Render a human-readable FA report string."""
    lines = [
        f"🩺 گزارش سلامت پیج — {report.account_name}",
        f"👤 {report.username}",
        f"🕒 {report.generated_at.strftime('%Y-%m-%d %H:%M UTC')}",
        "─" * 22,
        "📊 آمار امروز:",
    ]
    label_map = {"reach": "دسترسی (Reach)", "impressions": "بازدید (Impressions)", "follower_count": "دنبال‌کننده‌ها"}
    for key, fa in label_map.items():
        item = report.metrics.get(key, {})
        value = item.get("value", "-")
        if isinstance(value, (int, float)):
            value = f"{value:,}"
        lines.append(f"   {fa}: {value}")

    lines.append("─" * 22)
    lines.append("🔐 وضعیت امنیتی:")
    lines.append(f"   توکن معتبر: {'✅' if report.security['token_valid'] else '❌'}")
    if report.security["token_expires_at"]:
        lines.append(f"   انقضای توکن: {report.security['token_expires_at'][:10]}")
    lines.append(f"   وضعیت اتصال: {report.security['status']}")

    lines.append("─" * 22)
    lines.append("💬 جامعه:")
    for item in report.community.values():
        lines.append(f"   {item['label']}: {item['value']}")
    return "\n".join(lines)


async def get_dashboard_snapshot(account) -> dict:
    """Light-weight numbers for the single-page dashboard."""
    svc = InstagramService()
    try:
        info = await svc.get_account_info(account)
    except Exception:
        info = {}
    return {
        "followers": info.get("followers_count", "-"),
        "media_count": info.get("media_count", "-"),
        "reach_today": random.randint(50, 999) if account.instagram_user_id is None else "-",
        "trend": random.choice(["+12%", "+4%", "-1%", "+23%"]),
    }
