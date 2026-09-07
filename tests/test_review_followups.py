"""Некритичные находки код-ревью.

- Продление не закрывало VPN-провайдеров — текла aiohttp-сессия на каждый тап.
- `parse_price_kopecks` роняла не-ValueError на inf/nan и не имела верхней
  границы, из-за чего запись в SQLite падала OverflowError.
- Ввод админа возвращался в HTML-сообщение без экранирования.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from app.database.models import (
    AWGInbound,
    AWGInboundConnection,
    Client,
    Server,
    Subscription,
)
from app.services.pricing import MAX_PRICE_KOPECKS, parse_price_kopecks
from app.services.renewal_service import RenewalService


class TestParseRejectsSpecials:
    """Все отказы должны быть ValueError — хэндлеры ловят только его."""

    @pytest.mark.parametrize("value", ["inf", "Infinity", "-inf", "nan", "NaN", "snan"])
    def test_rejects_non_finite(self, value):
        with pytest.raises(ValueError):
            parse_price_kopecks(value)

    def test_rejects_absurdly_large(self):
        with pytest.raises(ValueError):
            parse_price_kopecks("1e1000")

    def test_rejects_above_limit(self):
        with pytest.raises(ValueError):
            parse_price_kopecks(str(MAX_PRICE_KOPECKS // 100 + 1))

    def test_accepts_limit(self):
        assert parse_price_kopecks(str(MAX_PRICE_KOPECKS // 100)) == MAX_PRICE_KOPECKS

    def test_error_message_does_not_leak_raw_input(self):
        """Текст ошибки уходит в HTML-сообщение, сырой ввод туда попадать не должен."""
        with pytest.raises(ValueError) as exc:
            parse_price_kopecks("<b>")
        assert "<b>" not in str(exc.value)


@pytest.mark.asyncio
async def test_renew_closes_providers(test_session, mock_settings, monkeypatch):
    """Иначе на каждое продление остаётся незакрытая aiohttp-сессия к панели."""
    server = Server(name="S-close", ip_address="1.2.3.4", is_active=True)
    test_session.add(server)
    await test_session.flush()
    inbound = AWGInbound(server_id=server.id, remark="r", protocol="p", is_active=True)
    client = Client(
        name="C", email="close@example.com", telegram_id=995001, is_admin=False, is_active=True
    )
    test_session.add_all([inbound, client])
    await test_session.flush()
    sub = Subscription(
        client_id=client.id,
        name="sub",
        subscription_token="tok-close",
        total_gb=0,
        price_kopecks=0,
        expiry_date=datetime.now(UTC) + timedelta(days=2),
        is_active=True,
    )
    test_session.add(sub)
    await test_session.flush()
    test_session.add(
        AWGInboundConnection(
            subscription_id=sub.id,
            inbound_id=inbound.id,
            is_enabled=True,
            public_key="pk",
            expiry_date=sub.expiry_date,
        )
    )
    await test_session.flush()

    provider = AsyncMock()
    provider.update_client = AsyncMock(return_value=True)
    provider.enable_client = AsyncMock(return_value=True)
    provider.close = AsyncMock()
    monkeypatch.setattr(
        "app.services.new_subscription_service.get_vpn_provider", lambda *a, **k: provider
    )

    await RenewalService(test_session).renew(sub.id, sub.expiry_date)

    provider.close.assert_awaited()
