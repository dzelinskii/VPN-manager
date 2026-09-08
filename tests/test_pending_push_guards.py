"""Защита признака pending_push и уведомления о застрявших изменениях.

Признак означает «в строке лежит желаемое состояние, панель его не получила».
Снять его без применения — значит потерять изменение: реконсиляция лечит
статус `error`, поэтому понижать до него нельзя, а восстановление клиента на
панели не применяет чужое непрошедшее изменение на соседнем inbound.
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
    Server,
    Subscription,
    XUIInbound,
    XUIInboundConnection,
)
from app.services.new_subscription_service import NewSubscriptionService
from app.services.protocol_sync.xui_sync import XUIProtocolSync

PENDING_PUSH = "pending_push"


async def _setup(session, uid, *, xui=True, conn_enabled=True, status=PENDING_PUSH):
    server = Server(name=f"S{uid}", ip_address="1.2.3.4", is_active=True)
    session.add(server)
    await session.flush()

    if xui:
        inbound = XUIInbound(
            server_id=server.id, remark="r", protocol="p", is_active=True, xui_id=1
        )
    else:
        inbound = AWGInbound(server_id=server.id, remark="r", protocol="p", is_active=True)
    client = Client(
        name="C", email=f"c{uid}@example.com", telegram_id=uid, is_admin=False, is_active=True
    )
    session.add_all([inbound, client])
    await session.flush()

    sub = Subscription(
        client_id=client.id,
        name="sub",
        subscription_token=f"tok{uid}",
        total_gb=10,
        expiry_date=datetime.now(UTC) + timedelta(days=30),
        is_active=True,
    )
    session.add(sub)
    await session.flush()

    common = {
        "subscription_id": sub.id,
        "inbound_id": inbound.id,
        "is_enabled": conn_enabled,
        "expiry_date": sub.expiry_date,
        "sync_status": status,
    }
    if xui:
        conn = XUIInboundConnection(
            **common, email=f"u{uid}@example.com", uuid=f"uuid-{uid}"
        )
    else:
        conn = AWGInboundConnection(**common, public_key="pk")
    session.add(conn)
    await session.flush()
    return sub, conn


def _panel(*conns, empty=False):
    clients = [] if empty else [
        {"id": c.uuid, "enable": True, "totalGB": 0, "expiryTime": 0} for c in conns
    ]
    xui_client = AsyncMock()
    xui_client.get_inbound = AsyncMock(
        return_value=SimpleNamespace(settings=json.dumps({"clients": clients}))
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


# --- Пути, снимавшие признак без применения -------------------------------


@pytest.mark.asyncio
async def test_divergence_restore_keeps_pending_push(test_session, mock_settings):
    """Восстановление клиента на панели не применяет чужое изменение на другом inbound.

    `_restore_on_panel` перевыбирает строки по (subscription_id, email) и помечает
    их применёнными — но соседняя строка может ждать отправки своего изменения.
    """
    from app.database.models import PendingDivergence
    from app.services.divergence_service import DivergenceService

    sub, conn = await _setup(test_session, 999002)
    inbound_row = await _load_inbound(test_session, conn.inbound_id)

    second_inbound = XUIInbound(
        server_id=inbound_row.server_id, remark="r2", protocol="p", is_active=True, xui_id=2
    )
    test_session.add(second_inbound)
    await test_session.flush()
    neighbour = XUIInboundConnection(
        subscription_id=sub.id,
        inbound_id=second_inbound.id,
        is_enabled=True,
        email=conn.email,
        uuid="uuid-999002-b",
        expiry_date=sub.expiry_date,
        sync_status="error",
    )
    test_session.add(neighbour)
    await test_session.flush()

    pd = PendingDivergence(
        batch_id="batch-999002",
        server_id=inbound_row.server_id,
        subscription_id=sub.id,
        email=conn.email,
        kind="missing",
        status="pending",
        details_json={"uuid": conn.uuid, "enable": True, "total_gb": 10},
    )
    test_session.add(pd)
    await test_session.flush()

    xui_client = AsyncMock()
    xui_client.add_client = AsyncMock(return_value=True)
    await DivergenceService(test_session)._restore_on_panel(pd, xui_client)

    assert conn.sync_status == PENDING_PUSH, "непрошедшее изменение помечено применённым"
    assert neighbour.sync_status == "synced"


# --- Уведомления о застрявших ---------------------------------------------


@pytest.mark.asyncio
async def test_stuck_notification_is_sent_once(test_session, mock_settings, monkeypatch):
    """Цикл идёт каждые несколько минут — уведомлять надо на переходе, а не всегда."""
    _, conn = await _setup(test_session, 999003)
    notify = AsyncMock()
    monkeypatch.setattr(
        "app.services.notification_service.NotificationService."
        "notify_admins_stuck_pending_push",
        notify,
    )
    provider = AsyncMock()
    provider.update_client = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "app.services.vpn_providers.factory.get_vpn_provider", lambda *a, **k: provider
    )

    other = SimpleNamespace(uuid="uuid-someone-else")
    inbound = await _load_inbound(test_session, conn.inbound_id)
    for _ in range(3):
        await XUIProtocolSync().sync_clients(
            test_session, inbound, xui_service=_panel(other)
        )

    assert notify.await_count == 1, f"уведомление ушло {notify.await_count} раз(а)"


@pytest.mark.asyncio
async def test_empty_panel_snapshot_does_not_alarm(
    test_session, mock_settings, monkeypatch
):
    """Пустой снимок не отличить от сбоя панели — выводов по нему не делаем."""
    _, conn = await _setup(test_session, 999004)
    notify = AsyncMock()
    monkeypatch.setattr(
        "app.services.notification_service.NotificationService."
        "notify_admins_stuck_pending_push",
        notify,
    )

    inbound = await _load_inbound(test_session, conn.inbound_id)
    await XUIProtocolSync().sync_clients(
        test_session, inbound, xui_service=_panel(empty=True)
    )

    notify.assert_not_awaited()
    assert conn.sync_status == PENDING_PUSH


# --- Индивидуальное выключение --------------------------------------------


@pytest.mark.asyncio
async def test_traffic_change_keeps_connection_disabled(
    test_session, mock_settings, monkeypatch
):
    """Правка трафика не должна воскрешать индивидуально выключенное подключение.

    Иначе досылка отправит enable=True на панель, и клиент оживёт.
    """
    sub, conn = await _setup(test_session, 999005, conn_enabled=False, status="synced")
    provider = AsyncMock()
    provider.update_client = AsyncMock(return_value=True)
    provider.close = AsyncMock()
    monkeypatch.setattr(
        "app.services.new_subscription_service.get_vpn_provider", lambda *a, **k: provider
    )

    await NewSubscriptionService(test_session).update_subscription(sub.id, total_gb=100)

    assert conn.is_enabled is False, "выключенное подключение включено правкой трафика"


@pytest.mark.asyncio
async def test_traffic_change_does_not_toggle_awg(
    test_session, mock_settings, monkeypatch
):
    """То же для AWG: правка трафика не должна дёргать enable/disable."""
    sub, conn = await _setup(
        test_session, 999006, xui=False, conn_enabled=False, status="synced"
    )
    provider = AsyncMock()
    provider.update_client = AsyncMock(return_value=True)
    provider.enable_client = AsyncMock(return_value=True)
    provider.disable_client = AsyncMock(return_value=True)
    provider.close = AsyncMock()
    monkeypatch.setattr(
        "app.services.new_subscription_service.get_vpn_provider", lambda *a, **k: provider
    )

    await NewSubscriptionService(test_session).update_subscription(sub.id, total_gb=100)

    provider.enable_client.assert_not_awaited()
    assert conn.is_enabled is False


@pytest.mark.asyncio
async def test_status_change_still_applies(test_session, mock_settings, monkeypatch):
    """Смена статуса подписки по-прежнему доходит до подключения."""
    sub, conn = await _setup(test_session, 999007, conn_enabled=True, status="synced")
    provider = AsyncMock()
    provider.update_client = AsyncMock(return_value=True)
    provider.close = AsyncMock()
    monkeypatch.setattr(
        "app.services.new_subscription_service.get_vpn_provider", lambda *a, **k: provider
    )

    await NewSubscriptionService(test_session).update_subscription(sub.id, is_active=False)

    assert conn.is_enabled is False


# --- Отражение в интерфейсе ------------------------------------------------


def test_pending_push_has_its_own_icon():
    """Строка в pending_push хранит намерение — рисовать её как применённую нельзя."""
    from types import SimpleNamespace

    from app.bot.handlers.admin.subscriptions import _conn_status_icon

    assert _conn_status_icon(SimpleNamespace(is_enabled=True, sync_status="synced")) == "✅"
    assert _conn_status_icon(SimpleNamespace(is_enabled=False, sync_status="synced")) == "❌"
    assert _conn_status_icon(SimpleNamespace(is_enabled=True, sync_status="pending_push")) == "⏳"
    assert _conn_status_icon(SimpleNamespace(is_enabled=False, sync_status="pending_push")) == "⏳"
