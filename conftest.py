"""Root pytest guard — tests must NEVER run against a real/remote database.

Incident 2026-09-22: a shell exported the PRODUCTION Supabase DATABASE_URL,
and because test modules use ``os.environ.setdefault(...)`` the suite ran
against the live DB and wiped real rows. This guard hard-overrides the URL
BEFORE any test module imports (conftest.py loads first), so `setdefault`
keeps the safe sqlite value. Do not remove.
"""

import os

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./tests_guard.db"
