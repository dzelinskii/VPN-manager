"""Записи об оплате продлений подписок."""

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.database.models.client import Client
    from app.database.models.subscription import Subscription


class Payment(Base, TimestampMixin):
    """Оплата продления.

    Пишется на каждое продление, включая бесплатное (сумма 0): так
    идемпотентность и история продлений живут в одном месте.

    Связи с подпиской и клиентом обнуляются, а не каскадят: удаление подписки
    не должно стирать историю денег.
    """

    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    subscription_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("subscriptions.id", ondelete="SET NULL"), nullable=True
    )
    client_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("clients.id", ondelete="SET NULL"), nullable=True
    )

    amount_kopecks: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="RUB", nullable=False)
    method: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="succeeded", nullable=False)
    period_days: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(200), unique=True, nullable=True)

    subscription: Mapped["Subscription | None"] = relationship("Subscription")
    client: Mapped["Client | None"] = relationship("Client")

    def __repr__(self) -> str:
        return (
            f"<Payment(id={self.id}, subscription_id={self.subscription_id}, "
            f"amount_kopecks={self.amount_kopecks}, method={self.method!r})>"
        )
