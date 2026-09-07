"""Разбор, форматирование и запись цены."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.database.models import Client, Subscription, SubscriptionTemplate
from app.services.pricing import (
    format_price,
    parse_price_kopecks,
    set_subscription_price,
    set_template_price,
)


class TestParse:
    def test_parses_rubles_with_kopecks(self):
        assert parse_price_kopecks("349.90") == 34990

    def test_accepts_comma_as_separator(self):
        assert parse_price_kopecks("349,90") == 34990

    def test_parses_whole_rubles(self):
        assert parse_price_kopecks("350") == 35000

    def test_zero_is_valid(self):
        """0 — осознанно бесплатная подписка, а не ошибка ввода."""
        assert parse_price_kopecks("0") == 0

    def test_strips_spaces(self):
        assert parse_price_kopecks("  199  ") == 19900

    def test_rejects_negative(self):
        with pytest.raises(ValueError):
            parse_price_kopecks("-100")

    def test_rejects_non_numeric(self):
        with pytest.raises(ValueError):
            parse_price_kopecks("дорого")

    def test_rejects_more_than_two_decimals(self):
        """Копейка — минимальная единица, дробить её некуда."""
        with pytest.raises(ValueError):
            parse_price_kopecks("10.999")


class TestFormat:
    def test_formats_rubles(self):
        assert format_price(34990) == "349.90 ₽"

    def test_marks_free(self):
        assert format_price(0) == "бесплатно"

    def test_marks_unset(self):
        assert format_price(None) == "цена не задана"


@pytest.mark.asyncio
async def test_set_template_price_sets_and_clears(test_session):
    template = SubscriptionTemplate(name="tpl-price", default_total_gb=0)
    test_session.add(template)
    await test_session.flush()

    assert await set_template_price(test_session, template.id, 34990) is True
    assert template.price_kopecks == 34990

    assert await set_template_price(test_session, template.id, None) is True
    assert template.price_kopecks is None


@pytest.mark.asyncio
async def test_set_template_price_reports_missing(test_session):
    assert await set_template_price(test_session, 999999, 100) is False


@pytest.mark.asyncio
async def test_set_subscription_price_sets_and_clears(test_session):
    client = Client(
        name="C", email="price@example.com", telegram_id=993001, is_admin=False, is_active=True
    )
    test_session.add(client)
    await test_session.flush()
    sub = Subscription(
        client_id=client.id,
        name="s",
        subscription_token="tok-price",
        total_gb=0,
        expiry_date=datetime.now(UTC) + timedelta(days=1),
        is_active=True,
    )
    test_session.add(sub)
    await test_session.flush()

    assert await set_subscription_price(test_session, sub.id, 0) is True
    stored = (
        await test_session.execute(select(Subscription).where(Subscription.id == sub.id))
    ).scalar_one()
    assert stored.price_kopecks == 0, "0 должен сохраняться, а не превращаться в NULL"

    assert await set_subscription_price(test_session, sub.id, None) is True
    assert stored.price_kopecks is None
