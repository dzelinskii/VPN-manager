"""Server monitor service for checking host availability via TCP probe."""

import asyncio
import contextlib

from loguru import logger

from app.database import async_session_factory
from app.database.models import Server


class ServerMonitor:
    """Checks server availability via TCP connect to the configured SSH port.

    ICMP-пинг не подходит: хостеры часто режут echo-request на сетевом уровне
    (например, правилом iptables перед цепочками UFW), из-за чего бот отказывался
    подключаться по SSH к живому серверу. TCP-подключение к SSH-порту — честный
    сигнал «SSH-служба доступна», без subprocess и платформенных веток.
    """

    @staticmethod
    async def check_tcp(host: str, port: int, timeout: float = 3.0) -> bool:
        """Check if a TCP port is accepting connections.

        Args:
            host: IP address or domain name.
            port: TCP port (SSH port of the server).
            timeout: Timeout in seconds.

        Returns:
            True if the connection is accepted, False otherwise
            (timeout, connection refused, DNS failure, etc.).
        """
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=timeout
            )
        except Exception as e:
            logger.debug("TCP-проба {}:{} не удалась: {}", host, port, e)
            return False

        with contextlib.suppress(Exception):
            writer.close()
            await writer.wait_closed()
        return True

    @classmethod
    async def check_server_status(cls, server_id: int) -> bool:
        """Probe a specific server and update its status in DB.

        Args:
            server_id: Server model ID

        Returns:
            True if online, False otherwise
        """
        async with async_session_factory() as session:
            server = await session.get(Server, server_id)
            if not server:
                return False

            # If ip_address is provided use it
            if server.ip_address:
                host = server.ip_address
                # Strip http:// or https:// if present
                if host.startswith("http://"):
                    host = host[7:]
                elif host.startswith("https://"):
                    host = host[8:]
                # Strip port if present
                if ":" in host:
                    host = host.split(":")[0]
            else:
                return False

            if not host:
                return False

            is_online = await cls.check_tcp(host, server.ssh_port or 22)

            # Update DB if status changed
            if server.is_online != is_online:
                server.is_online = is_online
                await session.commit()

            return is_online
