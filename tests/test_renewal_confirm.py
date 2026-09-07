"""Продление подписки без заданной цены требует подтверждения.

Иначе платному клиенту легко случайно продлить за ноль: пометка в списке есть,
но кнопка срабатывает молча.

Проверяется поведение обработчика, а не наличие подстрок в исходнике: подменяем
фабрику сессий на тестовую и вызываем хэндлер с поддельным callback'ом.
"""

import contextlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.bot.handlers.admin import renewals
from app.database.models import (
    AWGInbound,
    AWGInboundConnection,
    Client,
    Payment,
    Server,
    Subscription,
)


class _SessionProxy:
    """Отдаёт хэндлеру тестовую сессию, сводя commit к flush.

    Иначе коммит уходит в общую in-memory базу и данные теста протекают в
    соседние тесты — откат фикстуры их уже не уберёт.
    """

    def __init__(self, session) -> None:
        self._session = session

    def __getattr__(self, name):
        return getattr(self._session, name)

    async def commit(self):
        await self._session.flush()

    async def close(self):
        pass


def _session_factory(session):
    @contextlib.asynccontextmanager
    async def _factory():
        yield _SessionProxy(session)

    return _factory


class _FakeMessage:
    def __init__(self) -> None:
        self.text: str | None = None

    async def edit_text(self, text, **kwargs):
        self.text = text


class _FakeCallback:
    def __init__(self, data: str) -> None:
        self.data = data
        self.message = _FakeMessage()
        self.answers: list[str] = []

    async def answer(self, text: str = "", **kwargs):
        self.answers.append(text)


async def _make_subscription(session, uid, price):
    server = Server(name=f"S{uid}", ip_address="1.2.3.4", is_active=True)
    session.add(server)
    await session.flush()
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
        total_gb=0,
        price_kopecks=price,
        expiry_date=datetime.now(UTC) + timedelta(days=2),
        is_active=True,
    )
    session.add(sub)
    await session.flush()
    session.add(
        AWGInboundConnection(
            subscription_id=sub.id,
            inbound_id=inbound.id,
            is_enabled=True,
            public_key="pk",
            expiry_date=sub.expiry_date,
        )
    )
    await session.flush()
    return sub


@pytest.mark.asyncio
async def test_unset_price_asks_before_renewing(test_session, mock_settings, monkeypatch):
    """Цена не задана — показываем предупреждение и не продлеваем."""
    sub = await _make_subscription(test_session, 996001, price=None)
    monkeypatch.setattr(renewals, "async_session_factory", _session_factory(test_session))

    ts = int(sub.expiry_date.timestamp())
    callback = _FakeCallback(f"renew:do:{sub.id}:{ts}")
    await renewals.renew_subscription(callback)

    assert "не задана цена" in (callback.message.text or "")
    payments = (
        await test_session.execute(select(Payment).where(Payment.subscription_id == sub.id))
    ).scalars().all()
    assert payments == [], "продление не должно происходить до подтверждения"


@pytest.mark.asyncio
async def test_known_price_renews_without_asking(test_session, mock_settings, monkeypatch):
    """Цена известна — подтверждение не нужно, продлеваем сразу."""
    from unittest.mock import AsyncMock

    sub = await _make_subscription(test_session, 996002, price=34990)
    monkeypatch.setattr(renewals, "async_session_factory", _session_factory(test_session))

    provider = AsyncMock()
    provider.update_client = AsyncMock(return_value=True)
    provider.enable_client = AsyncMock(return_value=True)
    provider.close = AsyncMock()
    monkeypatch.setattr(
        "app.services.new_subscription_service.get_vpn_provider", lambda *a, **k: provider
    )

    ts = int(sub.expiry_date.timestamp())
    callback = _FakeCallback(f"renew:do:{sub.id}:{ts}")
    await renewals.renew_subscription(callback)

    # Предупреждение уходит в edit_text, а не в answer — проверяем именно его.
    assert "не задана цена" not in (callback.message.text or "")
    payments = (
        await test_session.execute(select(Payment).where(Payment.subscription_id == sub.id))
    ).scalars().all()
    assert len(payments) == 1
    assert payments[0].amount_kopecks == 34990
