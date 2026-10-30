"""Test sync service functionality."""

from datetime import UTC, datetime, timedelta

import pytest

from app.database.models import Client, Server, Subscription
from app.database.models.inbound import XUIInbound
from app.database.models.inbound_connection import XUIInboundConnection
from app.database.models.services import XUIPanel
from app.services.sync_service import SyncService


@pytest.mark.asyncio
async def test_sync_service_initialization(test_session, mock_settings):
    """Test that SyncService initializes correctly."""
    service = SyncService(test_session)
    assert service.session == test_session
    assert service._is_running is False


@pytest.mark.asyncio
async def test_needs_sync_new_model(test_session, mock_settings):
    """Test that new models need sync."""
    service = SyncService(test_session)

    # Create server without sync fields
    server = Server(
        name="TestServer",
        ip_address="test.com",
        is_active=True,
    )
    test_session.add(server)
    await test_session.flush()

    panel = XUIPanel(
        server_id=server.id,
        username="admin",
        password_encrypted="encrypted",
        panel_path="/",
    )
    test_session.add(panel)
    await test_session.flush()

    # Should need sync (last_sync_at is None)
    assert service._needs_sync(server) is True


@pytest.mark.asyncio
async def test_needs_sync_recent_sync(test_session, mock_settings):
    """Test that recently synced models don't need sync."""
    service = SyncService(test_session)

    # Create server with recent sync
    server = Server(
        name="TestServer",
        ip_address="test.com",
        is_active=True,
        last_sync_at=datetime.now(UTC),
        sync_status="synced",
    )
    test_session.add(server)
    await test_session.flush()

    panel = XUIPanel(
        server_id=server.id,
        username="admin",
        password_encrypted="encrypted",
    )
    test_session.add(panel)
    await test_session.flush()

    # Should not need sync (sync was recent)
    assert service._needs_sync(server) is False


@pytest.mark.asyncio
async def test_needs_sync_stale_sync(test_session, mock_settings):
    """Test that stale models need sync."""
    service = SyncService(test_session)

    # Create server with old sync
    old_sync_time = datetime.now(UTC) - timedelta(minutes=10)
    server = Server(
        name="TestServer",
        ip_address="test.com",
        is_active=True,
        last_sync_at=old_sync_time,
        sync_status="synced",
    )
    test_session.add(server)
    await test_session.flush()

    panel = XUIPanel(
        server_id=server.id,
        username="admin",
        password_encrypted="encrypted",
    )
    test_session.add(panel)
    await test_session.flush()

    # Should need sync (sync was more than 5 minutes ago)
    assert service._needs_sync(server) is True


@pytest.mark.asyncio
async def test_needs_sync_error_status(test_session, mock_settings):
    """Test that models with error status need sync."""
    service = SyncService(test_session)

    # Create server with error status
    server = Server(
        name="TestServer",
        ip_address="test.com",
        is_active=True,
        last_sync_at=datetime.now(UTC),
        sync_status="error",
        sync_error="Previous error",
    )
    test_session.add(server)
    await test_session.flush()

    panel = XUIPanel(
        server_id=server.id,
        username="admin",
        password_encrypted="encrypted",
    )
    test_session.add(panel)
    await test_session.flush()

    # Should need sync (status is error)
    assert service._needs_sync(server) is True


@pytest.mark.asyncio
async def test_needs_sync_offline_status(test_session, mock_settings):
    """Test that offline models need sync."""
    service = SyncService(test_session)

    # Create server with offline status
    server = Server(
        name="TestServer",
        ip_address="test.com",
        is_active=True,
        last_sync_at=datetime.now(UTC),
        sync_status="offline",
        sync_error="Connection failed",
    )
    test_session.add(server)
    await test_session.flush()

    panel = XUIPanel(
        server_id=server.id,
        username="admin",
        password_encrypted="encrypted",
    )
    test_session.add(panel)
    await test_session.flush()

    # Should need sync (status is offline)
    assert service._needs_sync(server) is True


@pytest.mark.asyncio
async def test_manual_sync_connection(test_session, mock_settings):
    """Test manual sync for connection."""
    service = SyncService(test_session)

    # Create inbound connection
    inbound = XUIInbound(
        id=1,
        server_id=1,
        xui_id=1,
        remark="TestInbound",
        protocol="vless",
        port=443,
        settings_json="{}",
        client_count=0,
        is_active=True,
    )
    test_session.add(inbound)

    client = Client(
        id=1,
        name="TestClient",
        email="test@client.com",
        telegram_id=123456789,
        is_admin=False,
        is_active=True,
    )
    test_session.add(client)

    subscription = Subscription(
        id=1,
        client_id=1,
        name="TestSub",
        subscription_token="test_token",
        total_gb=100,
        expiry_date=datetime.now(UTC) + timedelta(days=30),
        is_active=True,
    )
    test_session.add(subscription)

    connection = XUIInboundConnection(
        id=1,
        subscription_id=1,
        inbound_id=1,
        xui_client_id="uuid-1",
        email="test@vpn.local",
        uuid="uuid-1",
        is_enabled=True,
    )
    test_session.add(connection)
    await test_session.flush()

    # Manual sync should update sync status
    results = await service.manual_sync("connection", connection.id)
    assert results["synced"] == 1
    assert results["errors"] == 0


@pytest.mark.asyncio
async def test_manual_sync_all(test_session, mock_settings):
    """Test manual sync for all entities."""
    service = SyncService(test_session)

    # Create test server
    server = Server(
        name="TestServer",
        ip_address="test.com",
        is_active=True,
    )
    test_session.add(server)
    await test_session.flush()

    panel = XUIPanel(
        server_id=server.id,
        username="admin",
        password_encrypted="encrypted",
    )
    test_session.add(panel)
    await test_session.flush()

    # Manual sync should work (though may not actually sync anything)
    results = await service.manual_sync("all")
    assert "synced" in results
    assert "errors" in results


@pytest.mark.asyncio
async def test_sync_cycle_survives_failing_xui_server(test_session, mock_settings, monkeypatch):
    """Цикл синхронизации переживает ошибку XUI-сервера без MissingGreenlet.

    Регресс: в обработчиках ошибок sync_server обращался к server.id ПОСЛЕ
    session.rollback() (объект expired), а sync_all_servers — к server.name/.id
    после возврата. В async-SQLAlchemy это вызывало MissingGreenlet и роняло
    весь цикл (а последующие серверы оставались необработанными).
    """
    from unittest.mock import AsyncMock

    from app.services.server_monitor import ServerMonitor
    from app.xui_client import XUIError

    server_ids = []
    for i in (1, 2):
        server = Server(name=f"S{i}", ip_address=f"10.0.0.{i}", is_active=True)
        test_session.add(server)
        await test_session.flush()
        test_session.add(
            XUIPanel(
                server_id=server.id,
                url="https://example:8443/p",
                username="admin",
                password_encrypted="enc",
                panel_path="/p",
            )
        )
        server_ids.append(server.id)
    await test_session.commit()

    service = SyncService(test_session)
    # Сервер «онлайн», но XUI-клиент падает с XUIError (как при невалидном токене + пустом пароле).
    monkeypatch.setattr(ServerMonitor, "check_tcp", AsyncMock(return_value=True))
    monkeypatch.setattr(
        service._xui_service,
        "_get_client",
        AsyncMock(side_effect=XUIError("Login failed: Password is required")),
    )

    # Не должно бросать MissingGreenlet; должно вернуть число и обработать ОБА сервера.
    result = await service.sync_all_servers(force=True)
    assert isinstance(result, int)

    for sid in server_ids:
        srv = await test_session.get(Server, sid)
        await test_session.refresh(srv)
        assert srv.sync_status == "error", f"сервер {sid} не получил статус error"
        assert "Password is required" in (srv.sync_error or "")


@pytest.mark.asyncio
async def test_sync_server_inbounds_serializes_dict_settings(test_session, mock_settings):
    """Регресс: новый inbound из XUI приходит с settings в виде dict.

    Раньше ветка создания писала dict в settings_json как есть, из-за чего
    sqlite падал с ProgrammingError ("type 'dict' is not supported"). Теперь
    settings должен сериализоваться в JSON-строку, как и в ветке обновления.
    """
    import json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from sqlalchemy import select

    server = Server(name="S", ip_address="10.0.0.9", is_active=True)
    test_session.add(server)
    await test_session.flush()

    settings_dict = {
        "clients": [{"id": "d9b46e3d", "email": "cdn1", "enable": True}],
        "decryption": "none",
        "encryption": "none",
    }
    xui_ib = SimpleNamespace(
        id=8,
        settings=settings_dict,  # именно dict, а не строка
        remark="CDN-XHTTP",
        protocol="vless",
        port=2087,
    )
    fake_client = SimpleNamespace(get_inbounds=AsyncMock(return_value=[xui_ib]))

    service = SyncService(test_session)
    # Не должно бросать ProgrammingError
    await service._sync_server_inbounds(server, fake_client)
    await test_session.commit()

    row = (
        await test_session.execute(select(XUIInbound).where(XUIInbound.xui_id == 8))
    ).scalar_one()
    assert isinstance(row.settings_json, str)
    assert json.loads(row.settings_json) == settings_dict
    assert row.client_count == 1
