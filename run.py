"""Gramma — application entry point.

Usage:
  python run.py                 # bot (long polling) + in-process scheduler + API
  python run.py --no-api        # bot + scheduler only (API handled elsewhere)
  python run.py --no-scheduler  # bot + API without in-process jobs
  python run.py --create-tables # just initialize the database schema

The API server (port 8000) hosts the Mini-App backend and the Instagram
OAuth/webhook receiver. Telegram users always talk to the bot via
long-polling, so no public URL is required for the bot itself.
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from app.core.config import get_settings
from app.core.logging import setup_logging

setup_logging(get_settings().log_level)
logger = logging.getLogger("gramma")


async def _amain(args: argparse.Namespace) -> None:
    settings = get_settings()

    # 1) Database
    from app.core.database import init_db

    await init_db()
    logger.info("database ready: %s", settings.database_url[:40] + "…")

    # 2) Scheduler (in-process, when needed)
    scheduler = None
    if not args.no_scheduler:
        from app.workers.selector import should_run_inprocess_scheduler

        if await should_run_inprocess_scheduler():
            from app.workers.scheduler import build_scheduler

            scheduler = build_scheduler()
            scheduler.start()
            logger.info("in-process scheduler started")

    # 3) Bot (long polling) + optional API server
    from app.bot.main import get_bot, get_dispatcher

    bot = get_bot()
    dp = get_dispatcher()

    coros = [dp.start_polling(bot)]

    if not args.no_api:
        from app.webapp.server import serve

        coros.append(serve())

    logger.info("starting bot in long-polling mode…")
    try:
        await asyncio.gather(*coros)
    finally:
        if scheduler:
            scheduler.shutdown(wait=False)


def main() -> None:
    parser = argparse.ArgumentParser(description="Gramma bot runner")
    parser.add_argument("--no-api", action="store_true", help="do not start the Mini-App API server")
    parser.add_argument("--no-scheduler", action="store_true", help="disable the in-process scheduler")
    parser.add_argument("--create-tables", action="store_true", help="only create DB tables and exit")
    args = parser.parse_args()

    if args.create_tables:
        async def _create():
            from app.core.database import init_db

            await init_db()
            logger.info("tables created")

        asyncio.run(_create())
        return

    asyncio.run(_amain(args))


if __name__ == "__main__":
    main()
