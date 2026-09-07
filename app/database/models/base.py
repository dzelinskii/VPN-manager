"""Base model class for SQLAlchemy models."""

from datetime import datetime

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Base class for all database models."""

    pass


class TimestampMixin:
    """Mixin for created_at and updated_at timestamps."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class SyncMixin:
    """Mixin for synchronization tracking fields."""

    sync_status: Mapped[str] = mapped_column(
        String(20),
        default="synced",
        nullable=False,
    )  # "synced", "error", "offline", "pending_push"
    # "pending_push" — изменение записано в БД, но на сервер не доехало.
    # Отдельное от "error" значение: реконсиляция выбирает именно error-записи и
    # лечит те, чей клиент есть на панели, поэтому под тем статусом локальное
    # изменение было бы затёрто данными сервера на следующем цикле.
    last_sync_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    sync_error: Mapped[str] = mapped_column(
        Text,
        nullable=True,
    )
