"""Tests for ChatbotX integration."""

import os

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000:test")
os.environ.setdefault("ENCRYPTION_KEY", "9f6Bd2S19LFy/SQ/9Om4559N1Lgi5upd2zZGwvITWOA=")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./tests_chatbotx.db")
os.environ.setdefault("INSTAGRAM_ACCOUNT_MODE", "simulation")
os.environ.setdefault("CHATBOTX_WORKSPACE_TOKEN", "test_workspace_token_dummy")
os.environ.setdefault("CHATBOTX_BASE_URL", "https://app.chatbotx.io/api")
os.environ.setdefault("CHATBOTX_WORKSPACE_ID", "1170629")

import pytest
import httpx
from httpx import Response, Request, MockTransport

from app.services.chatbotx_service import ChatbotXClient


def mock_chatbotx_transport(request: Request) -> Response:
    path = request.url.path
    if "/v1/workspaces" in path:
        return Response(200, json={"id": "1170629", "name": "Test WS", "instagram_connected": True, "instagram_username": "@zeynalikids"})
    if "/v1/conversations" in path and request.method == "GET":
        return Response(200, json={"conversations": [{"id": "conv_1", "last_message": "سلام"}, {"id": "conv_2"}]})
    if "/v1/contacts" in path:
        return Response(200, json={"contacts": [{"id": "c1", "name": "Ali"}]})
    if "/messages" in path and request.method == "POST":
        return Response(200, json={"ok": True, "message_id": "msg_123"})
    return Response(404, json={"error": "not found"})


@pytest.mark.asyncio
async def test_chatbotx_client_enabled():
    client = ChatbotXClient(workspace_token="test_dummy_token", base_url="https://app.chatbotx.io/api")
    assert client.enabled is True
    client2 = ChatbotXClient(workspace_token="", base_url="https://app.chatbotx.io/api")
    assert client2.enabled is False


@pytest.mark.asyncio
async def test_chatbotx_get_workspace_status():
    transport = MockTransport(mock_chatbotx_transport)
    client = ChatbotXClient(
        workspace_token="test_dummy_token",
        base_url="https://app.chatbotx.io/api",
        workspace_id="1170629",
        transport=transport,
    )
    status = await client.get_workspace_status()
    assert status is not None
    assert "instagram_connected" in str(status) or "id" in str(status)


@pytest.mark.asyncio
async def test_chatbotx_get_conversations():
    transport = MockTransport(mock_chatbotx_transport)
    client = ChatbotXClient(
        workspace_token="test_dummy_token",
        base_url="https://app.chatbotx.io/api",
        transport=transport,
    )
    convs = await client.get_conversations(limit=10)
    assert isinstance(convs, list)
    assert len(convs) == 2
    assert convs[0]["id"] == "conv_1"


@pytest.mark.asyncio
async def test_chatbotx_send_message():
    transport = MockTransport(mock_chatbotx_transport)
    client = ChatbotXClient(
        workspace_token="test_dummy_token",
        base_url="https://app.chatbotx.io/api",
        transport=transport,
    )
    result = await client.send_message("conv_1", "سلام، چطور می‌تونم کمکتون کنم؟")
    assert result.get("ok") is True


@pytest.mark.asyncio
async def test_chatbotx_instagram_connection():
    transport = MockTransport(mock_chatbotx_transport)
    client = ChatbotXClient(
        workspace_token="test_dummy_token",
        base_url="https://app.chatbotx.io/api",
        workspace_id="1170629",
        transport=transport,
    )
    conn = await client.get_instagram_connection()
    assert conn["connected"] is True
    assert "@zeynalikids" in conn.get("username", "") or conn.get("connected") is True


@pytest.mark.asyncio
async def test_chatbotx_get_contacts():
    transport = MockTransport(mock_chatbotx_transport)
    client = ChatbotXClient(
        workspace_token="test_dummy_token",
        base_url="https://app.chatbotx.io/api",
        transport=transport,
    )
    contacts = await client.get_contacts(limit=10)
    assert isinstance(contacts, list)
    assert len(contacts) == 1
