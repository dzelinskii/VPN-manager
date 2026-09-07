"""Регресс: отключение подписки не доходило до сервера.

`update_subscription(is_active=False)` меняло только флаг в БД — блок применения
изменений открывался лишь при смене трафика или срока. Для AWG это означало, что
пир оставался и в конфиге, и в ядре: интерфейс показывал «отключено», а VPN
продолжал работать до следующего цикла фоновой синхронизации.

Вдобавок в цикле применения звался только `update_client`, который у AWG —
заглушка. Реальное включение/отключение делают `enable_client`/`disable_client`.
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
)
from app.database.models.inbound import XUIInbound
from app.database.models.inbound_connection import XUIInboundConnection
from app.services.new_subscription_service import NewSubscriptionService


async def _setup(
    session,
    uid,
    inbound_cls=AWGInbound,
    conn_cls=AWGInboundConnection,
    inbound_kwargs=None,
    **conn_kwargs,
):
    """Активная подписка с одним включённым подключением."""
    server = Server(name="S", ip_address="1.2.3.4", is_active=True)
    session.add(server)
    await session.flush()

    inbound = inbound_cls(
        server_id=server.id, remark="r", protocol="p", is_active=True, **(inbound_kwargs or {})
    )
    session.add(inbound)
    client = Client(
        name="C", email=f"c{uid}@example.com", telegram_id=uid, is_admin=False, is_active=True
    )
    session.add(client)
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

    conn = conn_cls(
        subscription_id=sub.id,
        inbound_id=inbound.id,
        is_enabled=True,
        expiry_date=datetime.now(UTC) + timedelta(days=30),
        **conn_kwargs,
    )
    session.add(conn)
    await session.flush()
    return sub, conn


def _provider() -> AsyncMock:
    provider = AsyncMock()
    provider.disable_client = AsyncMock(return_value=True)
    provider.enable_client = AsyncMock(return_value=True)
    provider.update_client = AsyncMock(return_value=True)
    provider.close = AsyncMock()
    return provider


@pytest.mark.asyncio
async def test_disable_subscription_disables_awg_on_server(
    test_session, mock_settings, monkeypatch
):
    """Отключение подписки должно снимать AWG-пир с сервера немедленно."""
    sub, conn = await _setup(test_session, 991001, public_key="pk")

    provider = _provider()
    monkeypatch.setattr(
        "app.services.new_subscription_service.get_vpn_provider", lambda *a, **k: provider
    )

    await NewSubscriptionService(test_session).update_subscription(sub.id, is_active=False)

    provider.disable_client.assert_awaited_once()
    assert conn.is_enabled is False


@pytest.mark.asyncio
async def test_enable_subscription_enables_awg_on_server(
    test_session, mock_settings, monkeypatch
):
    """Обратное включение подписки должно возвращать пир на сервер."""
    sub, conn = await _setup(test_session, 991002, public_key="pk")
    conn.is_enabled = False
    sub.is_active = False
    await test_session.flush()

    provider = _provider()
    monkeypatch.setattr(
        "app.services.new_subscription_service.get_vpn_provider", lambda *a, **k: provider
    )

    await NewSubscriptionService(test_session).update_subscription(sub.id, is_active=True)

    provider.enable_client.assert_awaited_once()
    assert conn.is_enabled is True


@pytest.mark.asyncio
async def test_disable_keeps_connection_enabled_when_server_refuses(
    test_session, mock_settings, monkeypatch
):
    """Если сервер не подтвердил отключение — в БД нельзя писать «отключено».

    Иначе бот считает клиента отключённым, а VPN работает: ровно тот класс бага,
    который эта правка и закрывает.
    """
    sub, conn = await _setup(test_session, 991003, public_key="pk")

    provider = _provider()
    provider.disable_client = AsyncMock(return_value=False)  # сервер отказал
    monkeypatch.setattr(
        "app.services.new_subscription_service.get_vpn_provider", lambda *a, **k: provider
    )

    await NewSubscriptionService(test_session).update_subscription(sub.id, is_active=False)

    assert conn.is_enabled is True, "нельзя помечать отключённым, если сервер не подтвердил"
    assert conn.sync_status == "error"


@pytest.mark.asyncio
async def test_xui_connection_still_goes_through_update_client(
    test_session, mock_settings, monkeypatch
):
    """XUI несёт флаг enable внутри update_client — этот путь менять нельзя."""
    sub, conn = await _setup(
        test_session,
        991004,
        inbound_cls=XUIInbound,
        conn_cls=XUIInboundConnection,
        inbound_kwargs={"xui_id": 1},
        email="u@example.com",
        uuid="11111111-1111-1111-1111-111111111111",
    )

    provider = _provider()
    monkeypatch.setattr(
        "app.services.new_subscription_service.get_vpn_provider", lambda *a, **k: provider
    )

    await NewSubscriptionService(test_session).update_subscription(sub.id, is_active=False)

    provider.update_client.assert_awaited_once()
    provider.disable_client.assert_not_awaited()
    assert conn.is_enabled is False


@pytest.mark.asyncio
async def test_toggle_connection_keeps_status_when_server_refuses(
    test_session, mock_settings, monkeypatch
):
    """Переключатель отдельного подключения: отказ сервера не должен менять флаг."""
    _, conn = await _setup(test_session, 991006, public_key="pk")

    provider = _provider()
    provider.disable_client = AsyncMock(return_value=False)
    monkeypatch.setattr(
        "app.services.new_subscription_service.get_vpn_provider", lambda *a, **k: provider
    )

    await NewSubscriptionService(test_session).toggle_inbound_connection(conn.id, False)

    assert conn.is_enabled is True
    assert conn.sync_status == "error"


@pytest.mark.asyncio
async def test_toggle_connection_applies_when_server_confirms(
    test_session, mock_settings, monkeypatch
):
    """При подтверждении сервером статус меняется как обычно."""
    _, conn = await _setup(test_session, 991007, public_key="pk")

    provider = _provider()
    monkeypatch.setattr(
        "app.services.new_subscription_service.get_vpn_provider", lambda *a, **k: provider
    )

    await NewSubscriptionService(test_session).toggle_inbound_connection(conn.id, False)

    provider.disable_client.assert_awaited_once()
    assert conn.is_enabled is False


@pytest.mark.asyncio
async def test_add_time_reenables_disabled_awg_connection(
    test_session, mock_settings, monkeypatch
):
    """Продление должно возвращать доступ отключённому по сроку клиенту.

    Это основной сценарий оплаты: деньги пришли — доступ включился.
    """
    sub, conn = await _setup(test_session, 991008, public_key="pk")
    conn.is_enabled = False
    await test_session.flush()

    provider = _provider()
    monkeypatch.setattr(
        "app.services.new_subscription_service.get_vpn_provider", lambda *a, **k: provider
    )

    await NewSubscriptionService(test_session).add_time_to_subscription(sub.id, 30)

    provider.enable_client.assert_awaited_once()
    assert conn.is_enabled is True


@pytest.mark.asyncio
async def test_add_time_keeps_disabled_when_enable_fails(
    test_session, mock_settings, monkeypatch
):
    """Если сервер не включил клиента — нельзя считать подписку рабочей.

    Иначе после оплаты бот покажет активный доступ, которого на сервере нет.
    """
    sub, conn = await _setup(test_session, 991009, public_key="pk")
    conn.is_enabled = False
    await test_session.flush()

    provider = _provider()
    provider.enable_client = AsyncMock(return_value=False)  # сервер не включил
    monkeypatch.setattr(
        "app.services.new_subscription_service.get_vpn_provider", lambda *a, **k: provider
    )

    await NewSubscriptionService(test_session).add_time_to_subscription(sub.id, 30)

    assert conn.is_enabled is False, "нельзя помечать включённым без подтверждения сервера"
    assert conn.sync_status == "error"


@pytest.mark.asyncio
async def test_name_only_update_does_not_touch_server(
    test_session, mock_settings, monkeypatch
):
    """Переименование подписки не должно дёргать провайдер."""
    sub, _ = await _setup(test_session, 991005, public_key="pk")

    provider = _provider()
    monkeypatch.setattr(
        "app.services.new_subscription_service.get_vpn_provider", lambda *a, **k: provider
    )

    await NewSubscriptionService(test_session).update_subscription(sub.id, name="новое имя")

    provider.disable_client.assert_not_awaited()
    provider.enable_client.assert_not_awaited()
    provider.update_client.assert_not_awaited()
    refreshed = (
        await test_session.execute(select(Subscription).where(Subscription.id == sub.id))
    ).scalar_one()
    assert refreshed.name == "новое имя"
