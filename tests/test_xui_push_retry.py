"""Досылка неотправленных изменений на XUI-панель.

Статус `pending_push` защищал локальное состояние от затирания данными панели,
но никто не пытался это состояние дослать: тихая потеря продления сменилась
тихим застреванием. Синхронизация теперь догоняет панель до состояния БД —
так же, как это давно делает `awg_sync`.

Источник истины при досылке — подписка: именно на ней живут срок, лимит и
признак активности. У подключения `is_enabled` после неудачного пуша отражает
панель, а не намерение админа.
"""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.database.models import (
    Client,
    Server,
    Subscription,
    XUIInbound,
    XUIInboundConnection,
)
from app.services.protocol_sync.xui_sync import XUIProtocolSync

PENDING_PUSH = "pending_push"


async def _setup(session, uid, *, sub_active=True, days_left=30, conn_enabled=True):
    server = Server(name=f"S{uid}", ip_address="1.2.3.4", is_active=True)
    session.add(server)
    await session.flush()

    inbound = XUIInbound(server_id=server.id, remark="r", protocol="p", is_active=True, xui_id=1)
    client = Client(
        name="C", email=f"c{uid}@example.com", telegram_id=uid, is_admin=False, is_active=True
    )
    session.add_all([inbound, client])
    await session.flush()

    sub = Subscription(
        client_id=client.id,
        name="sub",
        subscription_token=f"tok{uid}",
        total_gb=50,
        expiry_date=datetime.now(UTC) + timedelta(days=days_left),
        is_active=sub_active,
    )
    session.add(sub)
    await session.flush()

    conn = XUIInboundConnection(
        subscription_id=sub.id,
        inbound_id=inbound.id,
        is_enabled=conn_enabled,
        email=f"u{uid}@example.com",
        uuid=f"uuid-{uid}",
        expiry_date=sub.expiry_date,
        sync_status=PENDING_PUSH,
    )
    session.add(conn)
    await session.flush()
    return sub, conn


def _panel(conn, *, expiry_ms=0, enable=True):
    """Снимок панели, отстающий от БД."""
    xui_client = AsyncMock()
    xui_client.get_inbound = AsyncMock(
        return_value=SimpleNamespace(
            settings=json.dumps(
                {
                    "clients": [
                        {
                            "id": conn.uuid,
                            "enable": enable,
                            "totalGB": 0,
                            "expiryTime": expiry_ms,
                        }
                    ]
                }
            )
        )
    )
    service = AsyncMock()
    service._get_client = AsyncMock(return_value=xui_client)
    return service


async def _load_inbound(session, inbound_id):
    return (
        await session.execute(
            select(XUIInbound)
            .where(XUIInbound.id == inbound_id)
            .options(selectinload(XUIInbound.server))
        )
    ).scalar_one()


@pytest.mark.asyncio
async def test_pending_push_is_sent_to_panel(test_session, mock_settings, monkeypatch):
    """Успешная досылка снимает признак и подтягивает поля подписки."""
    sub, conn = await _setup(test_session, 998001)
    provider = AsyncMock()
    provider.update_client = AsyncMock(return_value=True)
    provider.close = AsyncMock()
    monkeypatch.setattr(
        "app.services.vpn_providers.factory.get_vpn_provider", lambda *a, **k: provider
    )

    inbound = await _load_inbound(test_session, conn.inbound_id)
    await XUIProtocolSync().sync_clients(
        test_session, inbound, xui_service=_panel(conn)
    )

    provider.update_client.assert_awaited_once()
    assert conn.sync_status == "synced"
    assert conn.expiry_date == sub.expiry_date
    assert conn.total_gb == sub.total_gb


@pytest.mark.asyncio
async def test_failed_push_keeps_pending(test_session, mock_settings, monkeypatch):
    """Панель не подтвердила — признак остаётся, повтор в следующем цикле."""
    sub, conn = await _setup(test_session, 998002)
    local_expiry = sub.expiry_date
    provider = AsyncMock()
    provider.update_client = AsyncMock(return_value=False)
    provider.close = AsyncMock()
    monkeypatch.setattr(
        "app.services.vpn_providers.factory.get_vpn_provider", lambda *a, **k: provider
    )

    stale_ms = int((datetime.now(UTC) - timedelta(days=1)).timestamp() * 1000)
    inbound = await _load_inbound(test_session, conn.inbound_id)
    await XUIProtocolSync().sync_clients(
        test_session, inbound, xui_service=_panel(conn, expiry_ms=stale_ms)
    )

    assert conn.sync_status == PENDING_PUSH
    assert sub.expiry_date == local_expiry, "локальный срок затёрт данными панели"


@pytest.mark.asyncio
async def test_push_raising_keeps_pending(test_session, mock_settings, monkeypatch):
    """Исключение провайдера не должно ронять весь цикл синхронизации."""
    _, conn = await _setup(test_session, 998003)
    provider = AsyncMock()
    provider.update_client = AsyncMock(side_effect=TimeoutError("panel unreachable"))
    provider.close = AsyncMock()
    monkeypatch.setattr(
        "app.services.vpn_providers.factory.get_vpn_provider", lambda *a, **k: provider
    )

    inbound = await _load_inbound(test_session, conn.inbound_id)
    synced = await XUIProtocolSync().sync_clients(
        test_session, inbound, xui_service=_panel(conn)
    )

    assert conn.sync_status == PENDING_PUSH
    assert synced == 0


@pytest.mark.asyncio
async def test_push_preserves_connection_intent(test_session, mock_settings, monkeypatch):
    """Досылается строка как есть, а не состояние, выведенное из подписки.

    У мультиинбаундной подписки подключение можно выключить индивидуально.
    Если досылка возьмёт `sub.is_active`, такое подключение включится обратно.
    """
    _, conn = await _setup(test_session, 998004, sub_active=True, conn_enabled=False)
    seen = {}

    async def _capture(inbound, connection, *args, **kwargs):
        seen["is_enabled"] = connection.is_enabled
        return True

    provider = AsyncMock()
    provider.update_client = AsyncMock(side_effect=_capture)
    monkeypatch.setattr(
        "app.services.vpn_providers.factory.get_vpn_provider", lambda *a, **k: provider
    )

    inbound = await _load_inbound(test_session, conn.inbound_id)
    await XUIProtocolSync().sync_clients(
        test_session, inbound, xui_service=_panel(conn, enable=False)
    )

    assert seen["is_enabled"] is False, "индивидуально выключенное подключение включено обратно"
    assert conn.is_enabled is False


@pytest.mark.asyncio
async def test_push_reuses_panel_client(test_session, mock_settings, monkeypatch):
    """Провайдер должен получить уже аутентифицированного клиента.

    Иначе он логинится в панель заново на каждый inbound, а цикл идёт каждые
    несколько минут.
    """
    _, conn = await _setup(test_session, 998008)
    provider = AsyncMock()
    provider.update_client = AsyncMock(return_value=True)
    provider._client = None
    monkeypatch.setattr(
        "app.services.vpn_providers.factory.get_vpn_provider", lambda *a, **k: provider
    )

    service = _panel(conn)
    expected_client = await service._get_client(None)

    inbound = await _load_inbound(test_session, conn.inbound_id)
    await XUIProtocolSync().sync_clients(test_session, inbound, xui_service=service)

    assert provider._client is expected_client, "провайдер логинится в панель заново"


@pytest.mark.asyncio
async def test_provider_created_once_per_inbound(test_session, mock_settings, monkeypatch):
    """Провайдер создаётся один раз на inbound, а не на каждое подключение.

    С одним застрявшим подключением это неотличимо, поэтому в очереди два.
    """
    sub, conn = await _setup(test_session, 998009)
    # Уникальность (subscription_id, inbound_id) — второму подключению на том же
    # inbound нужна своя подписка.
    sub2 = Subscription(
        client_id=sub.client_id,
        name="sub2",
        subscription_token="tok998009b",
        total_gb=10,
        expiry_date=sub.expiry_date,
        is_active=True,
    )
    test_session.add(sub2)
    await test_session.flush()
    second = XUIInboundConnection(
        subscription_id=sub2.id,
        inbound_id=conn.inbound_id,
        is_enabled=True,
        email="second@example.com",
        uuid="uuid-998009-b",
        expiry_date=sub.expiry_date,
        sync_status=PENDING_PUSH,
    )
    test_session.add(second)
    await test_session.flush()

    provider = AsyncMock()
    provider.update_client = AsyncMock(return_value=True)
    calls = {"n": 0}

    def _factory(*args, **kwargs):
        calls["n"] += 1
        return provider

    monkeypatch.setattr("app.services.vpn_providers.factory.get_vpn_provider", _factory)

    xui_client = AsyncMock()
    xui_client.get_inbound = AsyncMock(
        return_value=SimpleNamespace(
            settings=json.dumps(
                {
                    "clients": [
                        {"id": conn.uuid, "enable": True, "totalGB": 0, "expiryTime": 0},
                        {"id": second.uuid, "enable": True, "totalGB": 0, "expiryTime": 0},
                    ]
                }
            )
        )
    )
    service = AsyncMock()
    service._get_client = AsyncMock(return_value=xui_client)

    inbound = await _load_inbound(test_session, conn.inbound_id)
    await XUIProtocolSync().sync_clients(test_session, inbound, xui_service=service)

    assert provider.update_client.await_count == 2, "досланы не оба подключения"
    assert calls["n"] == 1, "провайдер создаётся на каждое подключение — лишние логины"


@pytest.mark.asyncio
async def test_missing_from_panel_is_logged(test_session, mock_settings, monkeypatch):
    """Дослать отсутствующего на панели нечем — такое не должно застревать молча.

    Реконсиляция его тоже не увидит: она выбирает подключения по статусу error.
    """
    from loguru import logger

    _, conn = await _setup(test_session, 998007)
    provider = AsyncMock()
    provider.update_client = AsyncMock(return_value=True)
    provider.close = AsyncMock()
    monkeypatch.setattr(
        "app.services.vpn_providers.factory.get_vpn_provider", lambda *a, **k: provider
    )

    notify = AsyncMock()
    monkeypatch.setattr(
        "app.services.notification_service.NotificationService."
        "notify_admins_missing_on_panel",
        notify,
    )

    # На панели другой клиент — нашего там нет.
    other = SimpleNamespace(uuid="uuid-someone-else")
    messages: list[str] = []
    sink = logger.add(lambda m: messages.append(str(m)), level="WARNING")
    try:
        inbound = await _load_inbound(test_session, conn.inbound_id)
        await XUIProtocolSync().sync_clients(
            test_session, inbound, xui_service=_panel(other)
        )
    finally:
        logger.remove(sink)

    provider.update_client.assert_not_awaited()
    assert any("отсутствует на панели" in m for m in messages), messages
    notify.assert_awaited_once(), "админам не ушло уведомление о застрявшем подключении"


@pytest.mark.asyncio
async def test_synced_connections_are_not_pushed(test_session, mock_settings, monkeypatch):
    """Обычные подключения досылкой не трогаются — иначе каждый цикл бил бы по панели."""
    _, conn = await _setup(test_session, 998006)
    conn.sync_status = "synced"
    await test_session.flush()

    provider = AsyncMock()
    provider.update_client = AsyncMock(return_value=True)
    provider.close = AsyncMock()
    monkeypatch.setattr(
        "app.services.vpn_providers.factory.get_vpn_provider", lambda *a, **k: provider
    )

    inbound = await _load_inbound(test_session, conn.inbound_id)
    await XUIProtocolSync().sync_clients(
        test_session, inbound, xui_service=_panel(conn)
    )

    provider.update_client.assert_not_awaited()
