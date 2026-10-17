"""Read-only Zernio checks. Intentionally NEVER invoke any publish/create method."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models import InstagramAccount, User  # also register the schema
from app.services import zernio_publishing as zernio
from app.webapp import main
from app.webapp.auth import MiniAppIdentity, resolve_miniapp_user


class AccountsOnlySDK:
    def __init__(self):
        self.accounts = SimpleNamespace(alist=self.list_accounts)
        self.posts = SimpleNamespace(acreate=self.no_posts)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def list_accounts(self):
        return SimpleNamespace(accounts=[
            SimpleNamespace(platform=SimpleNamespace(value="instagram"), field_id="ig_one", username="@zeynalikids", isActive=True),
            SimpleNamespace(platform=SimpleNamespace(value="instagram"), field_id="ig_other", username="other", isActive=True),
            SimpleNamespace(platform=SimpleNamespace(value="twitter"), field_id="x_one", username="other", isActive=True),
        ])

    async def no_posts(self, **_):
        raise AssertionError("No post may be created during read-only tests")


@pytest.mark.asyncio
async def test_official_sdk_account_listing_only():
    pub = zernio.ZernioPublisher(api_key="test_key", client_factory=AccountsOnlySDK)
    accounts = await pub.list_instagram_accounts()
    assert len(accounts) == 2
    assert accounts[0]["username"] == "@zeynalikids"
    assert all(a["platform"] == "instagram" for a in accounts)


@pytest.mark.asyncio
async def test_public_browser_links_are_side_effect_free(monkeypatch):
    monkeypatch.setattr(zernio, "get_zernio_publisher", lambda: (_ for _ in ()).throw(AssertionError("No SDK call on public GET")))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://testserver") as client:
        a = await client.get("/api/test-zernio")
        b = await client.get("/api/test-post")
        assert a.status_code == b.status_code == 200
        assert a.json()["read_only"] is True
        assert a.json()["post_sent"] is False
        assert b.json()["post_sent"] is False
        assert b.json()["automatic_publish"] is False
        assert (await client.get("/api/zernio/accounts")).status_code == 401


@pytest.fixture
async def db_setup(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        session.add_all([
            User(id=1, full_name="admin"), User(id=2, full_name="other"),
            InstagramAccount(id=10, owner_id=1, username="zeynalikids", status="connected"),
            InstagramAccount(id=11, owner_id=2, username="other", status="connected"),
        ])
        await session.commit()
    monkeypatch.setattr(main, "SessionLocal", sessions)
    monkeypatch.setattr(main.settings, "admin_telegram_ids", "1")
    monkeypatch.setattr(zernio, "get_zernio_publisher", lambda: zernio.ZernioPublisher(api_key="test_key", client_factory=AccountsOnlySDK))
    try:
        yield
    finally:
        main.app.dependency_overrides.clear()
        await engine.dispose()


@pytest.mark.asyncio
async def test_admin_read_only_filters_out_other_users_account(db_setup):
    main.app.dependency_overrides[resolve_miniapp_user] = lambda: MiniAppIdentity(user_id=1, expires_at=0)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://testserver") as client:
        response = await client.get("/api/zernio/accounts")
    assert response.status_code == 200
    assert response.json()["accounts"] == [{"id": "ig_one", "username": "@zeynalikids", "active": True, "local_account_id": 10}]


@pytest.mark.asyncio
async def test_non_admin_cannot_read_provider_accounts(db_setup):
    main.app.dependency_overrides[resolve_miniapp_user] = lambda: MiniAppIdentity(user_id=2, expires_at=0)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://testserver") as client:
        response = await client.get("/api/zernio/accounts")
    assert response.status_code == 403
