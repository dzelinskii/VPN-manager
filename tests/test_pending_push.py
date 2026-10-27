"""Признак «изменение записано, но на сервер не доехало».

Первая попытка защиты опиралась на sync_status="error", но реконсилятор
(`sync_service._reconcile_xui_server`) выбирает именно error-подключения и
безусловно лечит те, чей клиент есть на панели. Защита жила один проход, дальше
статус снимался и синхронизация откатывала локальные изменения по данным панели.

Отдельное значение `pending_push` в выборку реконсилятора не попадает, поэтому
не лечится, и синхронизация его уважает.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

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

PENDING_PUSH = "pending_push"


async def _setup(session, uid, xui=True, days_left=3):
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
    client = Client(
        name="C", email=f"c{uid}@example.com", telegram_id=uid, is_admin=False, is_active=True
    )
    session.add_all([inbound, client])
    await session.flush()

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
            email=f"u{uid}@example.com",
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


def _failing_provider():
    provider = AsyncMock()
    provider.update_client = AsyncMock(side_effect=TimeoutError("panel unreachable"))
    provider.enable_client = AsyncMock(return_value=True)
    provider.disable_client = AsyncMock(return_value=True)
    provider.close = AsyncMock()
    return provider


@pytest.mark.asyncio
async def test_reconciler_does_not_heal_pending_push(test_session, mock_settings):
    """Реконсилятор выбирает только error — pending_push он не видит и не лечит."""
    from app.services.sync_service import SyncService

    _, conn = await _setup(test_session, 997001)
    conn.sync_status = PENDING_PUSH
    await test_session.flush()

    xui_client = AsyncMock()
    xui_client.get_clients = AsyncMock(
        return_value=[{"email": conn.email, "id": conn.uuid, "enable": True}]
    )
    server = (
        await test_session.execute(select(Server).where(Server.name == "S997001"))
    ).scalar_one()

    await SyncService(test_session)._reconcile_xui_server(server, xui_client)

    assert conn.sync_status == PENDING_PUSH, "реконсилятор снял защиту"


@pytest.mark.asyncio
async def test_xui_sync_respects_pending_push(test_session, mock_settings, monkeypatch):
    """Синхронизация не принимает данные панели для непрошедшего изменения."""
    import json
    from types import SimpleNamespace

    from sqlalchemy.orm import selectinload

    from app.services.protocol_sync.xui_sync import XUIProtocolSync

    sub, conn = await _setup(test_session, 997002)
    local_expiry = datetime.now(UTC) + timedelta(days=33)
    sub.expiry_date = local_expiry
    conn.expiry_date = local_expiry
    conn.sync_status = PENDING_PUSH
    await test_session.flush()

    # Панель отказывает: досылка не проходит, значит защита обязана удержать
    # локальный срок, а не откатить его к панельному.
    push_provider = AsyncMock()
    push_provider.update_client = AsyncMock(return_value=False)
    push_provider.close = AsyncMock()
    monkeypatch.setattr(
        "app.services.vpn_providers.factory.get_vpn_provider", lambda *a, **k: push_provider
    )

    panel_ms = int((datetime.now(UTC) + timedelta(days=3)).timestamp() * 1000)
    xui_client = AsyncMock()
    xui_client.get_inbound = AsyncMock(
        return_value=SimpleNamespace(
            settings=json.dumps(
                {"clients": [{"id": conn.uuid, "enable": True, "totalGB": 0,
                              "expiryTime": panel_ms}]}
            )
        )
    )
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

    assert sub.expiry_date == local_expiry


@pytest.mark.asyncio
async def test_failed_renewal_marks_pending_push(test_session, mock_settings, monkeypatch):
    """Непрошедшее продление помечается pending_push, а не error.

    error реконсилятор вылечит, и локальный срок откатится по данным панели.
    """
    sub, conn = await _setup(test_session, 997003)
    monkeypatch.setattr(
        "app.services.new_subscription_service.get_vpn_provider",
        lambda *a, **k: _failing_provider(),
    )

    await NewSubscriptionService(test_session).add_time_to_subscription(sub.id, 30)

    assert conn.sync_status == PENDING_PUSH


@pytest.mark.asyncio
async def test_failed_status_change_marks_pending_push(
    test_session, mock_settings, monkeypatch
):
    sub, conn = await _setup(test_session, 997004)
    monkeypatch.setattr(
        "app.services.new_subscription_service.get_vpn_provider",
        lambda *a, **k: _failing_provider(),
    )

    await NewSubscriptionService(test_session).update_subscription(sub.id, is_active=False)

    assert conn.sync_status == PENDING_PUSH
    # Строка в pending_push хранит намерение админа, а не состояние панели:
    # именно его досылает синхронизация. Что оно ещё не применено, говорит статус.
    assert conn.is_enabled is False, "намерение должно сохраниться для досылки"


# --- C3: жёсткий отказ вместо тихого частичного успеха ---------------------


@pytest.mark.asyncio
async def test_toggle_all_raises_on_panel_failure(test_session, mock_settings, monkeypatch):
    """Раньше исключение улетало наверх и вызывающий откатывал транзакцию.

    Проглатывание превращало это в наполовину выполненную операцию с рапортом
    об успехе, поэтому отказ снова жёсткий.
    """
    sub, _ = await _setup(test_session, 997005)
    monkeypatch.setattr(
        "app.services.new_subscription_service.get_vpn_provider",
        lambda *a, **k: _failing_provider(),
    )

    with pytest.raises(Exception, match="panel unreachable"):
        await NewSubscriptionService(test_session).toggle_client_all_connections(
            sub.client_id, enable=False
        )


@pytest.mark.asyncio
async def test_toggle_single_raises_on_panel_failure(test_session, mock_settings, monkeypatch):
    _, conn = await _setup(test_session, 997006)
    monkeypatch.setattr(
        "app.services.new_subscription_service.get_vpn_provider",
        lambda *a, **k: _failing_provider(),
    )

    with pytest.raises(Exception, match="panel unreachable"):
        await NewSubscriptionService(test_session).toggle_inbound_connection(conn.id, False)
