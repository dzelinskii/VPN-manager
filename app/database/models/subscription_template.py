"""Subscription template model for predefined subscription configurations."""

from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.database.models.subscription_template_inbound import SubscriptionTemplateInbound


class SubscriptionTemplate(Base, TimestampMixin):
    """Predefined subscription template with multiple inbounds."""

    __tablename__ = "subscription_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    default_total_gb: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )  # 0 = unlimited
    default_expiry_days: Mapped[int | None] = mapped_column(Integer, nullable=True)  # None = never
    price_kopecks: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )  # цена за 30 дней; None = не задана
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_public: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Relationships
    template_inbounds: Mapped[list["SubscriptionTemplateInbound"]] = relationship(
        "SubscriptionTemplateInbound",
        back_populates="template",
        cascade="all, delete-orphan",
        order_by="SubscriptionTemplateInbound.order",
    )

    def __repr__(self) -> str:
        return f"<SubscriptionTemplate(id={self.id}, name='{self.name}')>"
