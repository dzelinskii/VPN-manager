"""Base VPN Provider interface."""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

from app.database.models import Inbound, InboundConnection, Server, Subscription


class BaseVPNProvider(ABC):
    """Abstract base class for all VPN Panel providers.

    All VPN providers (XUI, AmneziaWG, MTProxy) must implement this interface.
    Protocol-specific limitations:
    - AWG: enable/disable is kernel-level peer management; traffic via `awg show dump`
    - MTProxy: no traffic tracking, no per-user expiry
    - XUI: full REST API with all features
    """

    def __init__(self, server: Server) -> None:
        self.server = server

    @abstractmethod
    async def add_client(
        self,
        inbound: Inbound,
        subscription: Subscription,
        client_uuid: str | None = None,
        email: str | None = None,
    ) -> dict[str, Any]:
        """Add a new client to the VPN service.

        Returns:
            Dictionary with protocol-specific data (uuid, keys, secrets, etc.)
            to be stored in InboundConnection fields.
        """
        pass

    @abstractmethod
    async def remove_client(self, inbound: Inbound, connection: InboundConnection) -> bool:
        """Permanently remove a client and free their resources (IP, keys, etc.)."""
        pass

    @abstractmethod
    async def update_client(
        self,
        inbound: Inbound,
        connection: InboundConnection,
        new_total_gb: int | None = None,
        new_expiry_date: datetime | None = None,
    ) -> bool:
        """Update client settings (traffic limit, expiry).

        For protocols without traffic/expiry support (AWG, MTProxy),
        this is a no-op that returns True.
        """
        pass

    @abstractmethod
    async def enable_client(self, inbound: Inbound, connection: InboundConnection) -> bool:
        """Re-enable a previously disabled client.

        For AWG: re-adds peer to kernel with same keys/IP (config unchanged).
        For MTProxy: re-adds secret to config and restarts container.
        For XUI: updates client enable flag via API.
        """
        pass

    @abstractmethod
    async def disable_client(self, inbound: Inbound, connection: InboundConnection) -> bool:
        """Temporarily disable a client without deleting their data.

        For AWG: removes peer from kernel, keeps config entry and reserved IP.
        For MTProxy: removes secret from config (destructive, secret stored in DB).
        For XUI: updates client enable flag via API.
        """
        pass

    @abstractmethod
    async def get_client_config(
        self, inbound: Inbound, connection: InboundConnection, prefer_json: bool = True
    ) -> dict[str, Any]:
        """Get client configuration (links, files, QR codes).

        Returns:
            {
                "config_type": "link" | "file",
                "config_data": str,
                "filename": str (optional, for file type),
                "qr_code_base64": str (optional),
            }
        """
        pass

    @abstractmethod
    async def reset_client_traffic(
        self, inbound: Inbound, connection: InboundConnection
    ) -> bool:
        """Reset client traffic counters.

        For protocols without traffic tracking (AWG, MTProxy),
        this is a no-op that returns True.
        """
        pass

    @abstractmethod
    async def get_client_traffic(
        self, inbound: Inbound, connection: InboundConnection
    ) -> dict[str, Any] | None:
        """Get client traffic statistics.

        Returns:
            {"upload": int, "download": int, "total": int} in bytes,
            or None if the protocol doesn't support traffic tracking.
        """
        pass

    @abstractmethod
    async def close(self) -> None:
        """Release resources (HTTP sessions, SSH connections, etc.)."""
        pass
