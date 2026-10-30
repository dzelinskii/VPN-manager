"""Тесты TCP-пробы доступности сервера (ServerMonitor.check_tcp).

ICMP-пинг заменён на TCP-подключение к SSH-порту из настроек сервера:
хостеры часто режут echo-request на сетевом уровне, из-за чего preflight
отказывался подключаться по SSH к живому серверу.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.server_monitor import ServerMonitor


@pytest.mark.asyncio
async def test_check_tcp_success():
    """Открытый порт → True, соединение закрывается."""
    writer = MagicMock()
    writer.close = MagicMock()
    writer.wait_closed = AsyncMock()

    with patch("asyncio.open_connection", new=AsyncMock(return_value=(MagicMock(), writer))):
        assert await ServerMonitor.check_tcp("203.0.113.10", 2222) is True
    writer.close.assert_called_once()
    writer.wait_closed.assert_awaited_once()


@pytest.mark.asyncio
async def test_check_tcp_refused():
    with patch("asyncio.open_connection", new=AsyncMock(side_effect=ConnectionRefusedError())):
        assert await ServerMonitor.check_tcp("203.0.113.10", 22) is False


@pytest.mark.asyncio
async def test_check_tcp_timeout():
    async def slow_connect(*args, **kwargs):
        await asyncio.sleep(10)

    with patch("asyncio.open_connection", new=slow_connect):
        assert await ServerMonitor.check_tcp("203.0.113.10", 22, timeout=0.05) is False


@pytest.mark.asyncio
async def test_check_tcp_dns_failure():
    with patch("asyncio.open_connection", new=AsyncMock(side_effect=OSError("name resolution failed"))):
        assert await ServerMonitor.check_tcp("nonexistent.invalid", 22) is False


@pytest.mark.asyncio
async def test_preflight_probes_ssh_port():
    """Preflight пробует TCP на том же порту, что и SSH (а не ICMP и не хардкод 22)."""
    from app.services.installers.base import BaseInstaller
    from app.services.ssh_service import SSHManager

    ssh = MagicMock(spec=SSHManager)
    ssh.host = "203.0.113.10"
    ssh.port = 2222
    ssh.test_connection = AsyncMock(return_value=True)

    installer = BaseInstaller(ssh)
    installer._check_root_access = AsyncMock(return_value=(True, "root"))

    with patch("app.services.server_monitor.ServerMonitor.check_tcp", new=AsyncMock(return_value=True)) as probe:
        ok, msg = await installer.preflight_check()

    assert ok is True
    probe.assert_awaited_once_with("203.0.113.10", 2222)


@pytest.mark.asyncio
async def test_preflight_fails_when_tcp_closed():
    """Порт закрыт → False с сообщением о TCP-пробе; SSH даже не пробуем."""
    from app.services.installers.base import BaseInstaller
    from app.services.ssh_service import SSHManager

    ssh = MagicMock(spec=SSHManager)
    ssh.host = "203.0.113.10"
    ssh.port = 2222
    ssh.test_connection = AsyncMock(return_value=True)

    installer = BaseInstaller(ssh)

    with patch("app.services.server_monitor.ServerMonitor.check_tcp", new=AsyncMock(return_value=False)):
        ok, msg = await installer.preflight_check()

    assert ok is False
    assert "203.0.113.10:2222" in msg
    assert "TCP" in msg
    ssh.test_connection.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_uses_server_ssh_port():
    """Синк-цикл пробует порт из настроек сервера (ssh_port), а не дефолт."""
    from app.services.sync_service import SyncService

    server = MagicMock()
    server.id = 1
    server.name = "srv"
    server.ip_address = "203.0.113.10"
    server.ssh_port = 2222
    server.xui_panel = None

    inbounds_result = MagicMock()
    inbounds_result.scalars.return_value.all.return_value = []
    session = MagicMock()
    session.execute = AsyncMock(return_value=inbounds_result)
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()

    service = SyncService.__new__(SyncService)
    service.session = session
    service._xui_service = None

    with patch("app.services.server_monitor.ServerMonitor.check_tcp", new=AsyncMock(return_value=True)) as probe:
        await service.sync_server(server, force=True)

    probe.assert_awaited_once_with("203.0.113.10", 2222)
    assert server.is_online is True


@pytest.mark.asyncio
async def test_check_server_status_uses_db_port():
    """check_server_status берёт порт из строки сервера в БД."""
    server = MagicMock()
    server.ip_address = "203.0.113.10"
    server.ssh_port = 2222
    server.is_online = False

    session = MagicMock()
    session.get = AsyncMock(return_value=server)
    session.commit = AsyncMock()

    class _Ctx:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *args):
            return False

    factory = MagicMock(return_value=_Ctx())

    with patch("app.services.server_monitor.async_session_factory", factory), \
         patch("app.services.server_monitor.ServerMonitor.check_tcp", new=AsyncMock(return_value=True)) as probe:
        result = await ServerMonitor.check_server_status(1)

    assert result is True
    probe.assert_awaited_once_with("203.0.113.10", 2222)
    assert server.is_online is True
