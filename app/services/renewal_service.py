"""Продление подписок с учётом платежей.

Каждое продление создаёт запись в ``payments``. Ключ идемпотентности собран из
id подписки и прежней даты окончания, поэтому повторное нажатие по устаревшему
сообщению даёт конфликт вставки вместо второго продления.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from loguru import logger
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.models import InboundConnection, Payment, Subscription
from app.services.pricing import resolve_price

RENEWAL_PERIOD_DAYS = 30
DEFAULT_DUE_WINDOW_DAYS = 7


class AlreadyRenewedError(Exception):
    """Продление с этим ключом уже выполнено."""


class SubscriptionNotFoundError(Exception):
    """Подписка исчезла между отрисовкой списка и нажатием."""


class StaleRenewalError(Exception):
    """Состояние подписки изменилось после отрисовки списка.

    Ключ идемпотентности строится из даты окончания, поэтому продлевать по
    устаревшей кнопке нельзя: при сдвинутой дате получится второй платёж и
    двойной срок, при откаченной — конфликт ключа.
    """


@dataclass
class RenewalResult:
    """Итог продления."""

    subscription: Subscription
    amount_kopecks: int
    failed_connections: int


class RenewalService:
    """Список подписок к продлению и само продление."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_due(self, within_days: int = DEFAULT_DUE_WINDOW_DAYS) -> list[Subscription]:
        """Активные подписки, истекающие в окне (включая уже истёкшие).

        Бессрочные не возвращаются: продлевать нечего.
        """
        deadline = datetime.now(UTC) + timedelta(days=within_days)
        result = await self.session.execute(
            select(Subscription)
            .where(
                Subscription.is_active,
                Subscription.expiry_date.is_not(None),
                Subscription.expiry_date <= deadline,
            )
            .options(
                selectinload(Subscription.client),
                selectinload(Subscription.template),
            )
            .order_by(Subscription.expiry_date)
        )
        return list(result.scalars().all())

    async def renew(self, subscription_id: int, expected_expiry: datetime) -> RenewalResult:
        """Продлить подписку на 30 дней и записать платёж.

        ``expected_expiry`` — дата окончания, которая была показана админу. Она
        же формирует ключ идемпотентности, поэтому повтор по старому сообщению
        безопасен.
        """
        from app.services.new_subscription_service import NewSubscriptionService

        subscription = (
            await self.session.execute(
                select(Subscription)
                .where(Subscription.id == subscription_id)
                .options(selectinload(Subscription.template))
            )
        ).scalar_one_or_none()
        if subscription is None:
            raise SubscriptionNotFoundError(f"Подписка {subscription_id} не найдена")

        if not subscription.is_active:
            raise StaleRenewalError(f"Подписка {subscription_id} отключена")

        if subscription.expiry_date is None or self._seconds(
            subscription.expiry_date
        ) != self._seconds(expected_expiry):
            raise StaleRenewalError(
                f"Срок подписки {subscription_id} изменился после отрисовки списка"
            )

        amount = resolve_price(subscription) or 0
        key = self._idempotency_key(subscription_id, expected_expiry)

        payment = Payment(
            subscription_id=subscription.id,
            client_id=subscription.client_id,
            amount_kopecks=amount,
            currency="RUB",
            method="manual",
            status="succeeded",
            period_days=RENEWAL_PERIOD_DAYS,
            idempotency_key=key,
        )
        try:
            async with self.session.begin_nested():
                self.session.add(payment)
        except IntegrityError as e:
            logger.info("Продление {} уже выполнено (ключ {})", subscription_id, key)
            raise AlreadyRenewedError(key) from e

        # Провайдеры кэшируются внутри сервиса и держат aiohttp-сессии к панели,
        # поэтому закрываем их — пульт самый кликаемый экран.
        service = NewSubscriptionService(self.session)
        try:
            await service.add_time_to_subscription(subscription_id, RENEWAL_PERIOD_DAYS)
        finally:
            await service.close_all_clients()

        failed = await self._count_failed_connections(subscription_id)
        if failed:
            logger.warning(
                "Продление {}: {} подключений не применились на сервере",
                subscription_id, failed,
            )

        return RenewalResult(
            subscription=subscription, amount_kopecks=amount, failed_connections=failed
        )

    @staticmethod
    def _seconds(value: datetime) -> int:
        """Момент времени с точностью до секунды.

        Кнопка несёт целые секунды, в БД могут лежать микросекунды — сравнивать
        и строить ключ надо по общему знаменателю, иначе прод и тесты работают
        с разными ключами.
        """
        normalized = value if value.tzinfo else value.replace(tzinfo=UTC)
        return int(normalized.timestamp())

    @classmethod
    def _idempotency_key(cls, subscription_id: int, expiry: datetime) -> str:
        """Ключ вида ``manual:<id>:<unix-секунды прежней даты окончания>``."""
        return f"manual:{subscription_id}:{cls._seconds(expiry)}"

    async def _count_failed_connections(self, subscription_id: int) -> int:
        result = await self.session.execute(
            select(InboundConnection).where(
                InboundConnection.subscription_id == subscription_id,
                InboundConnection.sync_status.in_(("error", "pending_push")),
            )
        )
        return len(list(result.scalars().all()))
