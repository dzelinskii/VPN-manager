"""Модель платежа: запись продления и уникальность ключа идемпотентности."""

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.database.models import Payment


def _payment(key, amount=34990):
    return Payment(
        amount_kopecks=amount,
        currency="RUB",
        method="manual",
        status="succeeded",
        period_days=30,
        idempotency_key=key,
    )


@pytest.mark.asyncio
async def test_payment_is_stored(test_session):
    test_session.add(_payment("manual:1:2026-09-07T00:00:00+00:00"))
    await test_session.flush()

    stored = (await test_session.execute(select(Payment))).scalars().all()
    assert len(stored) == 1
    assert stored[0].amount_kopecks == 34990
    assert stored[0].currency == "RUB"


@pytest.mark.asyncio
async def test_idempotency_key_is_unique(test_session):
    """Второй платёж с тем же ключом вставиться не должен."""
    key = "manual:2:2026-09-07T00:00:00+00:00"
    test_session.add(_payment(key))
    await test_session.flush()

    test_session.add(_payment(key))
    with pytest.raises(IntegrityError):
        await test_session.flush()


@pytest.mark.asyncio
async def test_zero_amount_is_allowed(test_session):
    """Бесплатное продление тоже пишется — ради истории и идемпотентности."""
    test_session.add(_payment("manual:3:2026-09-07T00:00:00+00:00", amount=0))
    await test_session.flush()
