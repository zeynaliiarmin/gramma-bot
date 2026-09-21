"""Gramma — unified public API server (FastAPI + uvicorn).

One public port (8000) serving:
  * `/webhook/instagram[/callback]`  → Meta OAuth + webhook events
  * `/api/**`, `/ws/**`               → Mini-App backend
  * `/healthz`                        → liveness probe

Telegram updates are delivered separately (long-polling) so the bot itself
does not need a public URL; only Meta's OAuth/webhook requires one.
"""

from __future__ import annotations

import logging
import pathlib

import uvicorn
from fastapi.staticfiles import StaticFiles

from app.core.config import get_settings
from app.webhook.webhook import router as instagram_router
from app.webapp.main import app as miniapp_app

settings = get_settings()
logger = logging.getLogger("gramma.web")


def create_app():
    """Return the combined FastAPI app (webhook + Mini-App + SPA static).

    The Mini-App app is the base; we mount the Instagram webhook router and
    (when a built frontend exists) the static SPA onto it.
    """
    app = miniapp_app

    # 1) Instagram OAuth + webhook events
    app.include_router(instagram_router)

    # 2) Static SPA (optional, when a built frontend bundle is present)
    static_dir = pathlib.Path(__file__).parent / "static"
    if (static_dir / "index.html").exists():
        assets_dir = static_dir / "assets"
        if assets_dir.exists():
            app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="spa")

    return app


def run_server(host: str = "0.0.0.0", port: int = 8000) -> None:
    logger.info("starting unified API server on %s:%s", host, port)
    config = uvicorn.Config(
        create_app(), host=host, port=port, log_level="info", access_log=False
    )
    server = uvicorn.Server(config)
    server.run()


async def serve() -> None:
    """Async-coroutine form used by run.py (runs inside the same loop)."""
    logger.info("starting unified API server (async)")
    config = uvicorn.Config(
        create_app(), host="0.0.0.0", port=8000, log_level="info", access_log=False
    )
    server = uvicorn.Server(config)
    await server.serve()
