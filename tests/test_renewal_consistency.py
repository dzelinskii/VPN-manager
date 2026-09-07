"""Согласованность БД и сервера при продлении и смене статуса.

Три независимых способа разъехаться, найденных ревью:

1. Ключ идемпотентности строился из даты окончания, а фоновая синхронизация
   умеет откатывать её по данным панели — продление блокировалось навсегда.
2. XUI-ветка писала `is_enabled` до похода на панель: панель недоступна —
   в БД «отключено», на панели работает.
3. `toggle_client_all_connections` звала `update_client` до присвоения нового
   флага, а XUI-провайдер берёт `enable` из объекта — в панель уходил старый.
"""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.database.models import (
    AWGInbound,
    AWGInboundConnection,
    Client,
    Payment,
    Server,
    Subscription,
    XUIInbound,
    XUIInboundConnection,
)
from app.services.new_subscription_service import NewSubscriptionService
from app.services.renewal_service import RenewalService, StaleRenewalError


async def _client(session, uid):
    client = Client(
        name="C", email=f"c{uid}@example.com", telegram_id=uid, is_admin=False, is_active=True
    )
    session.add(client)
    await session.flush()
    return client


async def _setup(session, uid, xui=False, days_left=3):
    server = Server(name=f"S{uid}", ip_address="1.2.3.4", is_active=True)
    session.add(server)
    await session.flush()

    if xui:
        inbound = XUIInbound(
            server_id=server.id, remark="r", protocol="p", is_active=True, xui_id=1
        )
    else:
        inbound = AWGInbound(server_id=server.id, remark="r", protocol="p", is_active=True)
    session.add(inbound)
    client = await _client(session, uid)

    sub = Subscription(
        client_id=client.id,
        name="sub",
        subscription_token=f"tok{uid}",
        total_gb=0,
        price_kopecks=34990,
        expiry_date=datetime.now(UTC) + timedelta(days=days_left),
        is_active=True,
    )
    session.add(sub)
    await session.flush()

    if xui:
        conn = XUIInboundConnection(
            subscription_id=sub.id,
            inbound_id=inbound.id,
            is_enabled=True,
            email="u@example.com",
            uuid=f"uuid-{uid}",
            expiry_date=sub.expiry_date,
        )
    else:
        conn = AWGInboundConnection(
            subscription_id=sub.id,
            inbound_id=inbound.id,
            is_enabled=True,
            public_key="pk",
            expiry_date=sub.expiry_date,
        )
    session.add(conn)
    await session.flush()
    return sub, conn


def _provider():
    provider = AsyncMock()
    provider.update_client = AsyncMock(return_value=True)
    provider.enable_client = AsyncMock(return_value=True)
    provider.disable_client = AsyncMock(return_value=True)
    provider.close = AsyncMock()
    return provider


# --- C1: ключ идемпотентности и устаревший экран ---------------------------


@pytest.mark.asyncio
async def test_renew_rejects_stale_expiry(test_session, mock_settings, monkeypatch):
    """Если срок изменился после отрисовки списка — продлевать нельзя.

    Иначе продление через другой экран и возврат к устаревшему пульту дают
    второй платёж и двойной срок.
    """
    sub, _ = await _setup(test_session, 994001)
    stale = sub.expiry_date - timedelta(days=5)
    monkeypatch.setattr(
        "app.services.new_subscription_service.get_vpn_provider", lambda *a, **k: _provider()
    )

    before = sub.expiry_date
    with pytest.raises(StaleRenewalError):
        await RenewalService(test_session).renew(sub.id, stale)

    assert sub.expiry_date == before, "срок не должен меняться"
    payments = (await test_session.execute(select(Payment))).scalars().all()
    assert payments == [], "платёж не должен записываться"


@pytest.mark.asyncio
async def test_renew_tolerates_subsecond_difference(test_session, mock_settings, monkeypatch):
    """Кнопка несёт целые секунды, в БД могут быть микросекунды — это не «устарело»."""
    sub, _ = await _setup(test_session, 994002)
    truncated = datetime.fromtimestamp(int(sub.expiry_date.timestamp()), tz=UTC)
    monkeypatch.setattr(
        "app.services.new_subscription_service.get_vpn_provider", lambda *a, **k: _provider()
    )

    result = await RenewalService(test_session).renew(sub.id, truncated)

    assert result.amount_kopecks == 34990


@pytest.mark.asyncio
async def test_renew_rejects_inactive_subscription(test_session, mock_settings, monkeypatch):
    """Отключённую подписку устаревшая кнопка продлевать не должна."""
    sub, _ = await _setup(test_session, 994003)
    sub.is_active = False
    await test_session.flush()
    monkeypatch.setattr(
        "app.services.new_subscription_service.get_vpn_provider", lambda *a, **k: _provider()
    )

    with pytest.raises(StaleRenewalError):
        await RenewalService(test_session).renew(sub.id, sub.expiry_date)

    payments = (await test_session.execute(select(Payment))).scalars().all()
    assert payments == []


@pytest.mark.asyncio
async def test_xui_sync_does_not_revert_unpushed_expiry(test_session, mock_settings):
    """Синхронизация не должна затирать локальный срок, если push на панель упал.

    Иначе продление молча теряется, а вместе с ним освобождается ключ
    идемпотентности — и повтор упирается в «уже продлено».
    """
    from app.services.protocol_sync.xui_sync import XUIProtocolSync

    sub, conn = await _setup(test_session, 994004, xui=True)
    local_expiry = datetime.now(UTC) + timedelta(days=33)
    sub.expiry_date = local_expiry
    conn.expiry_date = local_expiry
    conn.sync_status = "pending_push"  # push на панель не прошёл
    await test_session.flush()

    panel_expiry_ms = int((datetime.now(UTC) + timedelta(days=3)).timestamp() * 1000)
    panel_inbound = SimpleNamespace(
        settings=json.dumps(
            {
                "clients": [
                    {
                        "id": conn.uuid,
                        "enable": True,
                        "totalGB": 0,
                        "expiryTime": panel_expiry_ms,
                    }
                ]
            }
        )
    )
    xui_client = AsyncMock()
    xui_client.get_inbound = AsyncMock(return_value=panel_inbound)
    xui_service = AsyncMock()
    xui_service._get_client = AsyncMock(return_value=xui_client)

    inbound = (
        await test_session.execute(
            select(XUIInbound)
            .where(XUIInbound.id == conn.inbound_id)
            .options(selectinload(XUIInbound.server))
        )
    ).scalar_one()
    await XUIProtocolSync().sync_clients(test_session, inbound, xui_service=xui_service)

    assert sub.expiry_date == local_expiry, "непрошедшее продление затёрто данными панели"


# --- C2: XUI-ветка смены статуса подписки ----------------------------------


@pytest.mark.asyncio
async def test_xui_status_not_flipped_when_panel_fails(test_session, mock_settings, monkeypatch):
    """Панель недоступна — в БД нельзя писать «отключено»."""
    sub, conn = await _setup(test_session, 994005, xui=True)
    provider = _provider()
    provider.update_client = AsyncMock(side_effect=TimeoutError("panel unreachable"))
    monkeypatch.setattr(
        "app.services.new_subscription_service.get_vpn_provider", lambda *a, **k: provider
    )

    await NewSubscriptionService(test_session).update_subscription(sub.id, is_active=False)

    assert conn.is_enabled is True, "флаг перевёрнут без подтверждения панели"
    assert conn.sync_status == "pending_push"


# --- C3: массовое переключение подключений клиента -------------------------


@pytest.mark.asyncio
async def test_toggle_all_sends_new_flag_to_panel(test_session, mock_settings, monkeypatch):
    """XUI-провайдер берёт enable из объекта — значит новое значение нужно до вызова."""
    sub, conn = await _setup(test_session, 994006, xui=True)
    seen = {}

    async def _capture(inbound, connection, *a, **k):
        seen["is_enabled"] = connection.is_enabled
        return True

    provider = _provider()
    provider.update_client = AsyncMock(side_effect=_capture)
    monkeypatch.setattr(
        "app.services.new_subscription_service.get_vpn_provider", lambda *a, **k: provider
    )

    await NewSubscriptionService(test_session).toggle_client_all_connections(
        sub.client_id, enable=False
    )

    assert seen["is_enabled"] is False, "в панель ушёл устаревший флаг"
    assert conn.is_enabled is False


@pytest.mark.asyncio
async def test_toggle_all_keeps_flag_when_panel_fails(test_session, mock_settings, monkeypatch):
    sub, conn = await _setup(test_session, 994007, xui=True)
    provider = _provider()
    provider.update_client = AsyncMock(side_effect=TimeoutError("panel unreachable"))
    monkeypatch.setattr(
        "app.services.new_subscription_service.get_vpn_provider", lambda *a, **k: provider
    )

    with pytest.raises(TimeoutError):
        await NewSubscriptionService(test_session).toggle_client_all_connections(
            sub.client_id, enable=False
        )

    assert conn.is_enabled is True, "флаг должен вернуться к прежнему значению"


@pytest.mark.asyncio
async def test_toggle_single_keeps_flag_when_panel_fails(test_session, mock_settings, monkeypatch):
    """Тот же дефект в переключателе одного подключения."""
    _, conn = await _setup(test_session, 994008, xui=True)
    provider = _provider()
    provider.update_client = AsyncMock(side_effect=TimeoutError("panel unreachable"))
    monkeypatch.setattr(
        "app.services.new_subscription_service.get_vpn_provider", lambda *a, **k: provider
    )

    with pytest.raises(TimeoutError):
        await NewSubscriptionService(test_session).toggle_inbound_connection(conn.id, False)

    assert conn.is_enabled is True, "флаг должен вернуться к прежнему значению"
