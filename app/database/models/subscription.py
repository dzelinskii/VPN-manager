"""Subscription model for client subscriptions."""

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.models.base import Base, TimestampMixin
from app.utils.date_utils import ensure_utc

if TYPE_CHECKING:
    from app.database.models.client import Client
    from app.database.models.inbound_connection import InboundConnection
    from app.database.models.subscription_template import SubscriptionTemplate


class Subscription(Base, TimestampMixin):
    """Client subscription (can have multiple inbounds)."""

    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    client_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("clients.id", ondelete="CASCADE"),
        nullable=False,
    )
    template_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("subscription_templates.id", ondelete="SET NULL"),
        nullable=True,
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    subscription_token: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        unique=True,
    )
    total_gb: Mapped[int] = mapped_column(Integer, default=0, nullable=False)  # 0 = unlimited
    price_kopecks: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )  # переопределяет цену шаблона; None = брать из шаблона, 0 = бесплатно
    expiry_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Relationships
    client: Mapped["Client"] = relationship("Client", back_populates="subscriptions")
    template: Mapped["SubscriptionTemplate | None"] = relationship("SubscriptionTemplate")
    inbound_connections: Mapped[list["InboundConnection"]] = relationship(
        "InboundConnection",
        back_populates="subscription",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<Subscription(id={self.id}, name='{self.name}')>"

    @property
    def is_expired(self) -> bool:
        """Check if subscription has expired."""
        expiry = ensure_utc(self.expiry_date)
        if expiry is None:
            return False
        return datetime.now(UTC) > expiry

    @property
    def is_unlimited(self) -> bool:
        """Check if subscription has unlimited traffic."""
        return self.total_gb == 0

    @property
    def remaining_days(self) -> int | None:
        """Calculate remaining days until expiry."""
        expiry = ensure_utc(self.expiry_date)
        if expiry is None:
            return None

        import math

        delta = expiry - datetime.now(UTC)
        return max(0, math.ceil(delta.total_seconds() / 86400))

    @property
    def active_connections_count(self) -> int:
        """Count active connections (enabled and not expired)."""
        return sum(1 for conn in self.inbound_connections if conn.is_connection_active)

    @property
    def expired_connections_count(self) -> int:
        """Count expired connections."""
        return sum(1 for conn in self.inbound_connections if conn.is_expired)

    @property
    def subscription_status(self) -> str:
        """Get subscription status based on connections."""
        if not self.is_active:
            return "❌ Неактивна"

        active_count = self.active_connections_count
        total_count = len(self.inbound_connections)

        if total_count == 0:
            return "❌ Нет подключений"
        elif active_count == total_count:
            return "✅ Активна"
        elif active_count > 0:
            return "⚠️ Частично"
        else:
            return "❌ Истекла"
