"""Продление подписки с записью платежа и защитой от двойного нажатия."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.database.models import (
    AWGInbound,
    AWGInboundConnection,
    Client,
    Payment,
    Server,
    Subscription,
    SubscriptionTemplate,
)
from app.services.renewal_service import AlreadyRenewedError, RenewalService


async def _setup(session, uid, days_left=3, price=34990, template_price=None):
    server = Server(name=f"S{uid}", ip_address="1.2.3.4", is_active=True)
    session.add(server)
    await session.flush()

    inbound = AWGInbound(server_id=server.id, remark="r", protocol="p", is_active=True)
    client = Client(
        name="C", email=f"c{uid}@example.com", telegram_id=uid, is_admin=False, is_active=True
    )
    template = SubscriptionTemplate(
        name=f"t{uid}", default_total_gb=0, price_kopecks=template_price
    )
    session.add_all([inbound, client, template])
    await session.flush()

    sub = Subscription(
        client_id=client.id,
        template_id=template.id,
        name="sub",
        subscription_token=f"tok{uid}",
        total_gb=0,
        price_kopecks=price,
        expiry_date=datetime.now(UTC) + timedelta(days=days_left),
        is_active=True,
    )
    session.add(sub)
    await session.flush()

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


@pytest.mark.asyncio
async def test_renew_extends_and_records_payment(test_session, mock_settings, monkeypatch):
    sub, _ = await _setup(test_session, 992001)
    old_expiry = sub.expiry_date
    monkeypatch.setattr(
        "app.services.new_subscription_service.get_vpn_provider", lambda *a, **k: _provider()
    )

    result = await RenewalService(test_session).renew(sub.id, old_expiry)

    assert result.amount_kopecks == 34990
    assert sub.expiry_date == old_expiry + timedelta(days=30)

    payments = (await test_session.execute(select(Payment))).scalars().all()
    assert len(payments) == 1
    assert payments[0].method == "manual"
    assert payments[0].period_days == 30
    assert payments[0].subscription_id == sub.id


@pytest.mark.asyncio
async def test_second_tap_does_not_extend_twice(test_session, mock_settings, monkeypatch):
    """Повтор по устаревшему сообщению не должен продлевать второй раз.

    После первого продления дата уехала вперёд, поэтому срабатывает проверка
    актуальности — она стоит раньше ключа идемпотентности.
    """
    from app.services.renewal_service import StaleRenewalError

    sub, _ = await _setup(test_session, 992002)
    old_expiry = sub.expiry_date
    monkeypatch.setattr(
        "app.services.new_subscription_service.get_vpn_provider", lambda *a, **k: _provider()
    )

    service = RenewalService(test_session)
    await service.renew(sub.id, old_expiry)
    expiry_after_first = sub.expiry_date

    with pytest.raises(StaleRenewalError):
        await service.renew(sub.id, old_expiry)

    assert sub.expiry_date == expiry_after_first
    payments = (await test_session.execute(select(Payment))).scalars().all()
    assert len(payments) == 1


@pytest.mark.asyncio
async def test_taken_idempotency_key_blocks_renewal(test_session, mock_settings, monkeypatch):
    """Занятый ключ останавливает продление — это защита от гонки двух тапов.

    Проверка актуальности ловит последовательный повтор, а ключ — параллельный,
    когда обе задачи прошли её с одинаковой датой.
    """
    sub, _ = await _setup(test_session, 992010)
    monkeypatch.setattr(
        "app.services.new_subscription_service.get_vpn_provider", lambda *a, **k: _provider()
    )

    service = RenewalService(test_session)
    test_session.add(
        Payment(
            subscription_id=sub.id,
            client_id=sub.client_id,
            amount_kopecks=34990,
            currency="RUB",
            method="manual",
            status="succeeded",
            period_days=30,
            idempotency_key=service._idempotency_key(sub.id, sub.expiry_date),
        )
    )
    await test_session.flush()
    before = sub.expiry_date

    with pytest.raises(AlreadyRenewedError):
        await service.renew(sub.id, sub.expiry_date)

    assert sub.expiry_date == before


@pytest.mark.asyncio
async def test_free_subscription_records_zero_payment(test_session, mock_settings, monkeypatch):
    sub, _ = await _setup(test_session, 992003, price=0)
    monkeypatch.setattr(
        "app.services.new_subscription_service.get_vpn_provider", lambda *a, **k: _provider()
    )

    result = await RenewalService(test_session).renew(sub.id, sub.expiry_date)

    assert result.amount_kopecks == 0
    payments = (await test_session.execute(select(Payment))).scalars().all()
    assert len(payments) == 1
    assert payments[0].amount_kopecks == 0


@pytest.mark.asyncio
async def test_price_falls_back_to_template(test_session, mock_settings, monkeypatch):
    sub, _ = await _setup(test_session, 992004, price=None, template_price=19900)
    monkeypatch.setattr(
        "app.services.new_subscription_service.get_vpn_provider", lambda *a, **k: _provider()
    )

    result = await RenewalService(test_session).renew(sub.id, sub.expiry_date)

    assert result.amount_kopecks == 19900


@pytest.mark.asyncio
async def test_partial_failure_is_reported(test_session, mock_settings, monkeypatch):
    """Платёж пишем, но о неприменённых подключениях сообщаем явно."""
    sub, conn = await _setup(test_session, 992005)
    provider = _provider()
    provider.update_client = AsyncMock(side_effect=RuntimeError("сервер недоступен"))
    monkeypatch.setattr(
        "app.services.new_subscription_service.get_vpn_provider", lambda *a, **k: provider
    )

    result = await RenewalService(test_session).renew(sub.id, sub.expiry_date)

    assert result.failed_connections == 1
    assert conn.sync_status == "error"
    payments = (await test_session.execute(select(Payment))).scalars().all()
    assert len(payments) == 1


@pytest.mark.asyncio
async def test_list_due_selects_only_expiring(test_session, mock_settings):
    """В список попадают истекающие и истёкшие, но не далёкие и не бессрочные."""
    soon, _ = await _setup(test_session, 992006, days_left=3)
    expired, _ = await _setup(test_session, 992007, days_left=-5)
    far, _ = await _setup(test_session, 992008, days_left=90)

    never, _ = await _setup(test_session, 992009, days_left=1)
    never.expiry_date = None
    await test_session.flush()

    due = await RenewalService(test_session).list_due(within_days=7)
    due_ids = [s.id for s in due]

    assert soon.id in due_ids
    assert expired.id in due_ids
    assert far.id not in due_ids
    assert never.id not in due_ids
    assert due_ids.index(expired.id) < due_ids.index(soon.id), "истёкшие идут первыми"
