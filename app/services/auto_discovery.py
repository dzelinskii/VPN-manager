"""Service for auto-discovering VPN services installed on a server via SSH.

Uses installer discover_existing() methods to read configuration from
vpnbot-* containers.
"""

from typing import Any

from loguru import logger

from app.database.models import Server
from app.services.ssh_service import SSHManager


class AutoDiscoveryService:
    """Discovers installed VPN services on a remote server."""

    # AWG-контейнеры, которые умеем обнаруживать: свой + контейнеры
    # официального приложения Amnezia (docs/amnezia-awg-parallel-management.md).
    AWG_CONTAINER_NAMES = ("vpnbot-awg", "amnezia-awg2", "amnezia-awg")

    def __init__(self, server: Server) -> None:
        self.server = server
        self.ssh = SSHManager(server)

    async def _all_containers(self) -> list[str]:
        """List all container names on the server (sudo-first, like before)."""
        try:
            output = await self.ssh.run_command(
                "sudo -n docker ps -a --format '{{.Names}}' 2>/dev/null || docker ps -a --format '{{.Names}}'"
            )
            return [name.strip() for name in output.strip().split("\n") if name.strip()]
        except Exception:
            return []

    async def discover_all(self) -> dict[str, dict[str, Any]]:
        """Run all discovery checks.

        Uses a single persistent SSH connection for all sub-discovery calls.

        Returns:
            Dict of discovered services and their details:
            {
                "3x-ui": {"domain": ..., "caddy_port": ..., "web_path": ..., ...},
                "amnezia-awg": {"port": ..., "subnet_ip": ..., ...},
                "mtproxy": {"port": ..., "implementation": ..., ...}
            }
        """
        async with self.ssh:
            containers = await self._all_containers()
            if not containers:
                return {}

            discovered: dict[str, dict[str, Any]] = {}

            if "vpnbot-xui" in containers:
                details = await self.discover_xui()
                if details:
                    discovered["3x-ui"] = details

            awg_container = next(
                (c for c in self.AWG_CONTAINER_NAMES if c in containers), None
            )
            if awg_container:
                details = await self.discover_amnezia_awg(awg_container)
                if details:
                    discovered["amnezia-awg"] = details

            if "vpnbot-mtproxy" in containers:
                details = await self.discover_mtproxy()
                if details:
                    discovered["mtproxy"] = details

        return discovered

    async def discover_xui(self) -> dict[str, Any] | None:
        """Discover 3x-ui + Caddy installation via XUIInstaller.discover_existing()."""
        try:
            from app.services.installers.xui_installer import XUIInstaller

            installer = XUIInstaller(self.ssh)
            return await installer.discover_existing()
        except Exception as e:
            logger.debug("XUI discovery не удался на сервере {}: {}", self.server.id, e)
            return None

    async def discover_amnezia_awg(
        self, container_name: str = "vpnbot-awg"
    ) -> dict[str, Any] | None:
        """Discover AmneziaWG installation via AWGInstaller.discover_existing().

        container_name позволяет прочитать и контейнер официального приложения
        (amnezia-awg2/amnezia-awg) — у него конфиг лежит в ФС контейнера.
        """
        try:
            from app.services.installers.awg_installer import AWGInstaller

            installer = AWGInstaller(self.ssh)
            return await installer.discover_existing(container_name)
        except Exception as e:
            logger.debug("AWG discovery не удался на сервере {}: {}", self.server.id, e)
            return None

    async def discover_mtproxy(self) -> dict[str, Any] | None:
        """Discover MTProxy installation via MTProxyInstaller.discover_existing()."""
        try:
            from app.services.installers.mtproxy_installer import MTProxyInstaller

            installer = MTProxyInstaller(self.ssh)
            return await installer.discover_existing()
        except Exception as e:
            logger.debug("MTProxy discovery не удался на сервере {}: {}", self.server.id, e)
            return None
