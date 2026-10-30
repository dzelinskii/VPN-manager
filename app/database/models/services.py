"""Subclasses for VPN Services."""

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import DateTime as SADateTime

from app.database.models.base import Base, TimestampMixin
from app.utils import decrypt_password, encrypt_password

if TYPE_CHECKING:
    from app.database.models.server import Server


class XUIPanel(Base, TimestampMixin):
    """3x-ui panel configuration."""

    __tablename__ = "xui_panels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    server_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("servers.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    server: Mapped["Server"] = relationship("Server", back_populates="xui_panel")

    # 3x-ui specific connections
    url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    username: Mapped[str | None] = mapped_column(String(100), nullable=True)
    password_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    verify_ssl: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Provider architecture
    panel_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    provider_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # Custom paths
    panel_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    subscription_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    subscription_json_path: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Installer params
    caddy_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    inbound_ranges: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # Session management
    session_cookies_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    session_created_at: Mapped[datetime | None] = mapped_column(
        SADateTime(timezone=True), nullable=True
    )

    # 2FA
    two_factor_code_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)

    # API Token auth (v3.1.0+)
    api_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    auth_mode: Mapped[str] = mapped_column(
        String(20), default="credentials", nullable=False
    )


class AWGService(Base, TimestampMixin):
    """AmneziaWG service configuration.

    ``server_private_key`` is stored encrypted (Fernet) in the DB.
    The Python property transparently encrypts on write and decrypts on read.
    """

    __tablename__ = "awg_services"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    server_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("servers.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    server: Mapped["Server"] = relationship("Server", back_populates="awg_service")

    port: Mapped[int] = mapped_column(Integer, nullable=False)
    subnet_ip: Mapped[str] = mapped_column(String(50), nullable=False, default="10.8.0.1")
    subnet_cidr: Mapped[int] = mapped_column(Integer, nullable=False, default=24)
    obfuscation: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    server_public_key: Mapped[str | None] = mapped_column(String(100), nullable=True)
    _server_private_key_encrypted: Mapped[str | None] = mapped_column(
        "server_private_key", String(250), nullable=True
    )

    @property
    def server_private_key(self) -> str | None:
        """Return decrypted server private key."""
        if self._server_private_key_encrypted is None:
            return None
        try:
            return decrypt_password(self._server_private_key_encrypted)
        except Exception:
            # Already plaintext (pre-migration rows) — return as-is
            return self._server_private_key_encrypted

    @server_private_key.setter
    def server_private_key(self, value: str | None) -> None:
        """Encrypt and store server private key."""
        if value is None:
            self._server_private_key_encrypted = None
        else:
            self._server_private_key_encrypted = encrypt_password(value)

    # Интероп с официальным клиентом Amnezia
    # (docs/amnezia-awg-parallel-management.md): container_name — имя docker-контейнера
    # (свой vpnbot-awg или внешний amnezia-awg2/amnezia-awg приложения); is_external —
    # контейнер установлен не ботом (переустановка из бота запрещена — она стирает
    # чужих пиров); wipe_detected — серверные ключи на сервере сменились (контейнер
    # переустановили извне), выдача конфигов блокируется до восстановления;
    # external_peers_seen — pubkey'и внешних пиров, о которых админ уже уведомлён.
    container_name: Mapped[str] = mapped_column(
        String(100), nullable=False, default="vpnbot-awg", server_default="vpnbot-awg"
    )
    is_external: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    wipe_detected: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    external_peers_seen: Mapped[list | None] = mapped_column(JSON, nullable=True)


class MTProxyService(Base, TimestampMixin):
    """MTProxy service configuration."""

    __tablename__ = "mtproxy_services"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    server_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("servers.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    server: Mapped["Server"] = relationship("Server", back_populates="mtproxy_service")

    implementation: Mapped[str] = mapped_column(String(20), nullable=False, default="mtg-multi")
    port: Mapped[int] = mapped_column(Integer, nullable=False, default=443)
    domain: Mapped[str] = mapped_column(String(200), nullable=False, default="google.com")
    max_connections: Mapped[int | None] = mapped_column(Integer, nullable=True)
    default_secret: Mapped[str | None] = mapped_column(String(200), nullable=True)
