"""Gramma — automatic database backups.

A nightly job (celery/APScheduler) rewrites a consistent SQLite snapshot for
SQLite deployments, or fires `pg_dump` for PostgreSQL, and keeps a rolling
set of N files under /backups. Secrets are never included: the backup is the
raw data file which already stores tokens AESE-256 encrypted at rest.
"""

from __future__ import annotations

import gzip
import logging
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from app.core.config import get_settings

settings = get_settings()
logger = logging.getLogger("gramma.backup")

# Configurable backup directory (defaults to ./backups inside the project).
BACKUP_DIR = Path(getattr(settings, "backup_dir", "") or "backups")
try:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
except OSError as exc:  # pragma: no cover - e.g. read-only filesystem
    BACKUP_DIR = Path("backups")
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
KEEP = 14  # rolling retention


async def run_backup() -> dict:
    """Create a dated backup file and prune old ones. Returns a summary."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    url = settings.database_url

    if url.startswith("sqlite"):
        src = url.split("///")[-1] or "./gramma.db"
        src_path = Path(src)
        if not src_path.exists():
            return {"status": "skipped", "reason": "no db file"}
        dest = BACKUP_DIR / f"gramma_{ts}.db.gz"
        with open(src_path, "rb") as f_in, gzip.open(dest, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)
    else:
        # PostgreSQL → pg_dump (assumes DATABASE_URL='postgresql+asyncpg://...')
        dest = BACKUP_DIR / f"gramma_{ts}.sql.gz"
        import re

        m = re.match(r"postgresql(?:\+asyncpg)?://([^:]+):([^@]+)@([^:/]+):?(\d+)?/(.+)", url)
        if not m:
            return {"status": "skipped", "reason": "unparsable postgres url"}
        user, password, host, port, db = m.groups()
        env = {"PGPASSWORD": password}
        cmd = [
            "pg_dump", "-h", host, "-p", port or "5432", "-U", user,
            "-Fc", db,
        ]
        with open(dest, "wb") as f_out:
            proc = subprocess.run(cmd, env=env, stdout=f_out, stderr=subprocess.PIPE)
        if proc.returncode != 0:
            dest.unlink(missing_ok=True)
            return {"status": "failed", "reason": proc.stderr.decode()[:200]}

    _prune()
    return {"status": "ok", "file": str(dest)}


def _prune() -> None:
    files = sorted(BACKUP_DIR.glob("gramma_*"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[KEEP:]:
        old.unlink(missing_ok=True)
        logger.info("pruned old backup %s", old.name)
