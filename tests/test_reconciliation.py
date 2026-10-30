"""Tests for zombie/phantom reconciliation and transaction boundaries in sync_service."""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_server(server_id: int = 1) -> MagicMock:
    server = MagicMock()
    server.id = server_id
    server.name = f"Server-{server_id}"
    server.ip_address = "10.0.0.1"
    server.is_online = True
    server.sync_status = "synced"
    server.sync_error = None
    xui_panel = MagicMock()
    xui_panel.url = "https://panel.example.com"
    xui_panel.username = "admin"
    server.xui_panel = xui_panel
    return server


def _make_xui_connection(
    conn_id: int = 1,
    email: str = "user@vpn",
    sync_status: str = "error",
    subscription_id: int = 10,
    inbound_id: int = 5,
) -> MagicMock:
    conn = MagicMock()
    conn.id = conn_id
    conn.email = email
    conn.sync_status = sync_status
    conn.subscription_id = subscription_id
    conn.inbound_id = inbound_id
    return conn


def _make_sync_service(session: MagicMock) -> MagicMock:
    """Build a real SyncService-like object without importing heavy deps."""
    from app.services.sync_service import SyncService

    svc = SyncService.__new__(SyncService)
    svc.session = session
    svc._is_running = False
    svc._xui_service = MagicMock()
    return svc


def _make_session() -> MagicMock:
    session = MagicMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.flush = AsyncMock()
    session.delete = MagicMock()
    return session


def _old_created_at_ms() -> int:
    """createdAt (epoch ms) older than grace-period — zombie should be deleted."""
    return int((time.time() - 20 * 60) * 1000)  # 20 minutes ago


def _fresh_created_at_ms() -> int:
    """createdAt (epoch ms) within grace-period — zombie must NOT be deleted."""
    return int(time.time() * 1000)  # right now


# ---------------------------------------------------------------------------
# 2a. Phantom tests
# ---------------------------------------------------------------------------


class TestPhantomReconciliation:
    """Фантомы БД: sync_status='error', клиента нет на панели → удалить."""

    @pytest.mark.asyncio
    async def test_phantom_deleted_when_absent_from_panel(self, mock_settings):
        """Соединение error + email отсутствует в снимке get_clients() → session.delete вызван.

        Снимок НЕПУСТОЙ: содержит другого клиента, ghost@vpn отсутствует → фантом удалён.
        """
        session = _make_session()

        conn = _make_xui_connection(conn_id=42, email="ghost@vpn", sync_status="error")

        from app.services.sync_service import SyncService

        svc = SyncService.__new__(SyncService)
        svc.session = session
        svc._xui_service = MagicMock()

        xui_client = MagicMock()
        # get_clients() snapshot НЕПУСТОЙ: ghost@vpn is NOT present, но другой клиент есть
        xui_client.get_clients = AsyncMock(
            return_value=[{"email": "other@vpn", "subId": "", "inboundIds": []}]
        )

        server = _make_server()

        # Mock session.execute: error_connections, sub_tokens, xui_inbounds, pairs
        execute_results = [
            _make_scalars_result([conn]),   # 2a: error connections
            _make_rows_result([]),          # 2b: subscription tokens
            _make_rows_result([]),          # 2b: xui inbounds
            _make_rows_result([]),          # 2b: existing pairs
            _make_scalars_result([]),       # 2c: synced connections
        ]
        session.execute = AsyncMock(side_effect=execute_results)

        await svc._reconcile_xui_server(server, xui_client)

        session.delete.assert_called_once_with(conn)

    @pytest.mark.asyncio
    async def test_phantom_restored_when_present_on_panel(self, mock_settings):
        """Соединение error + email ЕСТЬ в снимке get_clients() → sync_status='synced', не удалён."""
        session = _make_session()

        conn = _make_xui_connection(conn_id=7, email="alive@vpn", sync_status="error")

        from app.services.sync_service import SyncService

        svc = SyncService.__new__(SyncService)
        svc.session = session
        svc._xui_service = MagicMock()

        xui_client = MagicMock()
        # get_clients() snapshot: alive@vpn IS present
        xui_client.get_clients = AsyncMock(
            return_value=[{"email": "alive@vpn", "subId": "", "inboundIds": []}]
        )

        server = _make_server()

        execute_results = [
            _make_scalars_result([conn]),   # 2a: error connections
            _make_rows_result([]),          # 2b: subscription tokens
            _make_rows_result([]),          # 2b: xui inbounds
            _make_rows_result([]),          # 2b: existing pairs
            _make_scalars_result([]),       # 2c: synced connections
        ]
        session.execute = AsyncMock(side_effect=execute_results)

        await svc._reconcile_xui_server(server, xui_client)

        session.delete.assert_not_called()
        assert conn.sync_status == "synced"

    @pytest.mark.asyncio
    async def test_get_clients_raises_skips_entire_reconciliation(self, mock_settings):
        """get_clients() бросает → реконсиляция пропущена, ничего не удалено (ни зомби, ни фантомы)."""
        session = _make_session()

        from app.services.sync_service import SyncService

        svc = SyncService.__new__(SyncService)
        svc.session = session
        svc._xui_service = MagicMock()

        xui_client = MagicMock()
        xui_client.get_clients = AsyncMock(side_effect=Exception("connection refused"))

        server = _make_server()
        # execute should NOT be called because we return early
        session.execute = AsyncMock()

        await svc._reconcile_xui_server(server, xui_client)

        session.delete.assert_not_called()
        session.execute.assert_not_called()


# ---------------------------------------------------------------------------
# 2b. XUI zombie tests
# ---------------------------------------------------------------------------


class TestXUIZombieReconciliation:
    """XUI зомби: subId == токен существующей подписки, нет InboundConnection → удалить с панели."""

    @pytest.mark.asyncio
    async def test_zombie_deleted_when_old_enough(self, mock_settings):
        """subId совпадает с подпиской, нет connection, createdAt старый → delete_client вызван."""
        session = _make_session()

        from app.services.sync_service import SyncService

        svc = SyncService.__new__(SyncService)
        svc.session = session
        svc._xui_service = MagicMock()

        bot_token = "bot-token-123"
        panel_client = {
            "email": "zombie@vpn",
            "subId": bot_token,
            "inboundIds": [10],  # xui_id=10
            "createdAt": _old_created_at_ms(),
        }

        xui_client = MagicMock()
        xui_client.get_clients = AsyncMock(return_value=[panel_client])
        xui_client.delete_client = AsyncMock(return_value=True)

        server = _make_server()

        execute_results = [
            # 2a: error connections: none
            _make_scalars_result([]),
            # 2b: subscription tokens: bot_token → sub_id=99
            _make_rows_result([(bot_token, 99)]),
            # 2b: xui inbounds: xui_id=10 → inbound_db_id=5
            _make_rows_result([(10, 5)]),
            # 2b: existing pairs: no connection for (99, 5)
            _make_rows_result([]),
            # 2c: synced connections: none
            _make_scalars_result([]),
        ]
        session.execute = AsyncMock(side_effect=execute_results)

        await svc._reconcile_xui_server(server, xui_client)

        xui_client.delete_client.assert_called_once_with("zombie@vpn")

    @pytest.mark.asyncio
    async def test_phantom_attachment_detached_not_deleted(self, mock_settings):
        """Мультиинбаунд: у клиента есть валидный inbound + осиротевшая привязка →
        detach только осиротевшего, клиента НЕ удалять целиком (иначе снесёт валидный
        inbound — баг, из-за которого реконсилятор удалял живых клиентов)."""
        session = _make_session()

        from app.services.sync_service import SyncService

        svc = SyncService.__new__(SyncService)
        svc.session = session
        svc._xui_service = MagicMock()

        bot_token = "bot-token-multi"
        panel_client = {
            "email": "multi@vpn",
            "subId": bot_token,
            "inboundIds": [10, 20],  # xui_id 10 (валидный) + 20 (сирота)
            "createdAt": _old_created_at_ms(),
        }

        xui_client = MagicMock()
        xui_client.get_clients = AsyncMock(return_value=[panel_client])
        xui_client.detach_client = AsyncMock(return_value=True)
        xui_client.delete_client = AsyncMock(return_value=True)

        server = _make_server()

        execute_results = [
            _make_scalars_result([]),               # 2a: error connections
            _make_rows_result([(bot_token, 99)]),   # 2b: tokens → sub 99
            _make_rows_result([(10, 5), (20, 6)]),  # 2b: xui_id 10→db5, 20→db6
            _make_rows_result([(99, 5)]),           # 2b: pairs: только (99, 5) валиден
            _make_scalars_result([]),               # 2c: synced connections
        ]
        session.execute = AsyncMock(side_effect=execute_results)

        await svc._reconcile_xui_server(server, xui_client)

        xui_client.detach_client.assert_awaited_once_with("multi@vpn", [20])
        xui_client.delete_client.assert_not_called()

    @pytest.mark.asyncio
    async def test_zombie_skipped_within_grace_period(self, mock_settings):
        """Зомби моложе grace-period (createdAt=сейчас) → НЕ удалён."""
        session = _make_session()

        from app.services.sync_service import SyncService

        svc = SyncService.__new__(SyncService)
        svc.session = session
        svc._xui_service = MagicMock()

        bot_token = "fresh-token"
        panel_client = {
            "email": "fresh-zombie@vpn",
            "subId": bot_token,
            "inboundIds": [10],
            "createdAt": _fresh_created_at_ms(),  # right now — within grace period
        }

        xui_client = MagicMock()
        xui_client.get_clients = AsyncMock(return_value=[panel_client])
        xui_client.delete_client = AsyncMock(return_value=True)

        server = _make_server()

        execute_results = [
            _make_scalars_result([]),
            _make_rows_result([(bot_token, 99)]),
            _make_rows_result([(10, 5)]),
            _make_rows_result([]),  # no connection → would be zombie IF old enough
            _make_scalars_result([]),  # 2c: synced connections
        ]
        session.execute = AsyncMock(side_effect=execute_results)

        await svc._reconcile_xui_server(server, xui_client)

        xui_client.delete_client.assert_not_called()

    @pytest.mark.asyncio
    async def test_zombie_without_created_at_not_deleted(self, mock_settings):
        """Клиент без createdAt/0 → не удалён (безопаснее пропустить)."""
        session = _make_session()

        from app.services.sync_service import SyncService

        svc = SyncService.__new__(SyncService)
        svc.session = session
        svc._xui_service = MagicMock()

        bot_token = "no-ts-token"
        # No createdAt field at all
        panel_client = {
            "email": "nots@vpn",
            "subId": bot_token,
            "inboundIds": [10],
        }

        xui_client = MagicMock()
        xui_client.get_clients = AsyncMock(return_value=[panel_client])
        xui_client.delete_client = AsyncMock(return_value=True)

        server = _make_server()

        execute_results = [
            _make_scalars_result([]),
            _make_rows_result([(bot_token, 99)]),
            _make_rows_result([(10, 5)]),
            _make_rows_result([]),
            _make_scalars_result([]),  # 2c: synced connections
        ]
        session.execute = AsyncMock(side_effect=execute_results)

        await svc._reconcile_xui_server(server, xui_client)

        xui_client.delete_client.assert_not_called()

    @pytest.mark.asyncio
    async def test_zombie_with_zero_created_at_not_deleted(self, mock_settings):
        """Клиент с createdAt=0 → не удалён."""
        session = _make_session()

        from app.services.sync_service import SyncService

        svc = SyncService.__new__(SyncService)
        svc.session = session
        svc._xui_service = MagicMock()

        bot_token = "zero-ts-token"
        panel_client = {
            "email": "zeronts@vpn",
            "subId": bot_token,
            "inboundIds": [10],
            "createdAt": 0,
        }

        xui_client = MagicMock()
        xui_client.get_clients = AsyncMock(return_value=[panel_client])
        xui_client.delete_client = AsyncMock(return_value=True)

        server = _make_server()

        execute_results = [
            _make_scalars_result([]),
            _make_rows_result([(bot_token, 99)]),
            _make_rows_result([(10, 5)]),
            _make_rows_result([]),
            _make_scalars_result([]),  # 2c: synced connections
        ]
        session.execute = AsyncMock(side_effect=execute_results)

        await svc._reconcile_xui_server(server, xui_client)

        xui_client.delete_client.assert_not_called()

    @pytest.mark.asyncio
    async def test_zombie_empty_email_not_deleted(self, mock_settings):
        """Зомби с пустым email → не удалён."""
        session = _make_session()

        from app.services.sync_service import SyncService

        svc = SyncService.__new__(SyncService)
        svc.session = session
        svc._xui_service = MagicMock()

        bot_token = "bot-token-empty-email"
        panel_client = {
            "email": "",  # пустой email
            "subId": bot_token,
            "inboundIds": [10],
            "createdAt": _old_created_at_ms(),
        }

        xui_client = MagicMock()
        xui_client.get_clients = AsyncMock(return_value=[panel_client])
        xui_client.delete_client = AsyncMock(return_value=True)

        server = _make_server()

        execute_results = [
            _make_scalars_result([]),
            _make_rows_result([(bot_token, 99)]),
            _make_rows_result([(10, 5)]),
            _make_rows_result([]),
            _make_scalars_result([]),  # 2c: synced connections
        ]
        session.execute = AsyncMock(side_effect=execute_results)

        await svc._reconcile_xui_server(server, xui_client)

        xui_client.delete_client.assert_not_called()

    @pytest.mark.asyncio
    async def test_unknown_sub_id_not_deleted(self, mock_settings):
        """subId НЕ совпадает ни с одной подпиской → НЕ удалён, warning залогирован."""
        session = _make_session()

        from app.services.sync_service import SyncService

        svc = SyncService.__new__(SyncService)
        svc.session = session
        svc._xui_service = MagicMock()

        panel_client = {
            "email": "manual@vpn",
            "subId": "unknown-token-xyz",
            "inboundIds": [10],
            "createdAt": _old_created_at_ms(),
        }

        xui_client = MagicMock()
        xui_client.get_clients = AsyncMock(return_value=[panel_client])
        xui_client.delete_client = AsyncMock()

        server = _make_server()

        execute_results = [
            _make_scalars_result([]),       # 2a: no error connections
            _make_rows_result([("other-token", 1)]),  # 2b: DB has different token
            _make_rows_result([(10, 5)]),
            _make_rows_result([]),
            _make_scalars_result([]),       # 2c: synced connections
        ]
        session.execute = AsyncMock(side_effect=execute_results)

        with patch("app.services.sync_service.logger") as mock_logger:
            await svc._reconcile_xui_server(server, xui_client)

            xui_client.delete_client.assert_not_called()
            # Warning должен быть залогирован
            warning_calls = [str(c) for c in mock_logger.warning.call_args_list]
            assert any("unknown-token-xyz" in w or "manual@vpn" in w for w in warning_calls)

    @pytest.mark.asyncio
    async def test_client_without_sub_id_not_deleted(self, mock_settings):
        """Клиент без subId → не трогать (debug-лог, не удалять)."""
        session = _make_session()

        from app.services.sync_service import SyncService

        svc = SyncService.__new__(SyncService)
        svc.session = session
        svc._xui_service = MagicMock()

        panel_client = {
            "email": "nosub@vpn",
            "subId": "",  # пустой subId
            "inboundIds": [10],
            "createdAt": _old_created_at_ms(),
        }

        xui_client = MagicMock()
        xui_client.get_clients = AsyncMock(return_value=[panel_client])
        xui_client.delete_client = AsyncMock()

        server = _make_server()

        execute_results = [
            _make_scalars_result([]),
            _make_rows_result([("some-token", 1)]),
            _make_rows_result([(10, 5)]),
            _make_rows_result([]),
            _make_scalars_result([]),  # 2c: synced connections
        ]
        session.execute = AsyncMock(side_effect=execute_results)

        await svc._reconcile_xui_server(server, xui_client)

        xui_client.delete_client.assert_not_called()

    @pytest.mark.asyncio
    async def test_connected_client_not_deleted(self, mock_settings):
        """subId совпадает с подпиской И есть InboundConnection → клиент не удаляется."""
        session = _make_session()

        from app.services.sync_service import SyncService

        svc = SyncService.__new__(SyncService)
        svc.session = session
        svc._xui_service = MagicMock()

        bot_token = "connected-token"
        panel_client = {
            "email": "connected@vpn",
            "subId": bot_token,
            "inboundIds": [10],
            "createdAt": _old_created_at_ms(),
        }

        xui_client = MagicMock()
        xui_client.get_clients = AsyncMock(return_value=[panel_client])
        xui_client.delete_client = AsyncMock()

        server = _make_server()

        execute_results = [
            _make_scalars_result([]),
            _make_rows_result([(bot_token, 99)]),  # sub_id=99
            _make_rows_result([(10, 5)]),           # xui_id=10 → inbound_db_id=5
            _make_rows_result([(99, 5)]),           # pair (99,5) exists → NOT zombie
            _make_scalars_result([]),               # 2c: synced connections
        ]
        session.execute = AsyncMock(side_effect=execute_results)

        await svc._reconcile_xui_server(server, xui_client)

        xui_client.delete_client.assert_not_called()


# ---------------------------------------------------------------------------
# 2c. Mirror: synced connections manually deleted on the panel
# ---------------------------------------------------------------------------


def _make_synced_connection(
    conn_id: int = 20,
    email: str = "removed@vpn",
    subscription_id: int = 10,
    inbound_id: int = 5,
    last_sync_at=None,
    created_at=None,
) -> MagicMock:
    """Build a mock XUIInboundConnection with sync_status='synced'."""
    from datetime import UTC, datetime, timedelta

    conn = MagicMock()
    conn.id = conn_id
    conn.email = email
    conn.sync_status = "synced"
    conn.subscription_id = subscription_id
    conn.inbound_id = inbound_id

    if last_sync_at is None:
        # Default: old enough to be caught by grace-period check
        last_sync_at = datetime.now(UTC) - timedelta(minutes=30)
    conn.last_sync_at = last_sync_at

    if created_at is None:
        created_at = datetime.now(UTC) - timedelta(hours=2)
    conn.created_at = created_at

    # Mock subscription → client chain for user label
    client_mock = MagicMock()
    client_mock.name = "Test User"
    sub_mock = MagicMock()
    sub_mock.client = client_mock
    conn.subscription = sub_mock
    return conn


class TestMissingOnPanelReconciliation:
    """2c: synced-соединения, отсутствующие на панели — зеркалим в error."""

    @pytest.mark.asyncio
    async def test_synced_absent_old_marked_error_not_deleted(self, mock_settings):
        """synced + email НЕ в снимке + старше grace → sync_status='error', НЕ удалён, уведомление отправлено.

        Снимок НЕПУСТОЙ: содержит другого клиента, но НЕ removed@vpn.
        Тестирует сценарий ручного удаления клиента на панели при живых других клиентах.
        """
        from unittest.mock import AsyncMock, MagicMock, patch

        session = _make_session()

        conn = _make_synced_connection(conn_id=20, email="removed@vpn")

        from app.services.sync_service import SyncService

        svc = SyncService.__new__(SyncService)
        svc.session = session
        svc._xui_service = MagicMock()

        xui_client = MagicMock()
        # Снимок панели НЕПУСТОЙ: другой клиент есть, removed@vpn ОТСУТСТВУЕТ.
        # Это сценарий ручного удаления (не soft-fail API).
        xui_client.get_clients = AsyncMock(
            return_value=[{"email": "other@vpn", "subId": "", "inboundIds": []}]
        )

        server = _make_server()

        execute_results = [
            _make_scalars_result([]),        # 2a: нет error-соединений
            _make_rows_result([]),           # 2b: нет токенов
            _make_rows_result([]),           # 2b: нет xui inbounds
            _make_rows_result([]),           # 2b: нет пар
            _make_scalars_result([conn]),    # 2c: synced-соединение
        ]
        session.execute = AsyncMock(side_effect=execute_results)

        notify_mock = AsyncMock()
        with patch(
            "app.services.notification_service.NotificationService.notify_admins_missing_on_panel",
            new=notify_mock,
        ):
            await svc._reconcile_xui_server(server, xui_client)

        # Должно быть помечено как error
        assert conn.sync_status == "error"
        # НЕ удалено в этом проходе
        session.delete.assert_not_called()
        # Уведомление отправлено
        notify_mock.assert_awaited_once()
        call_kwargs = notify_mock.await_args
        assert call_kwargs is not None
        marked = call_kwargs.kwargs.get("marked_connections") or call_kwargs.args[1]
        assert any(entry["email"] == "removed@vpn" for entry in marked)

    @pytest.mark.asyncio
    async def test_synced_absent_fresh_not_marked(self, mock_settings):
        """synced + email НЕ в снимке + внутри grace-period → НЕ помечается."""
        from datetime import UTC, datetime, timedelta
        from unittest.mock import AsyncMock, MagicMock, patch

        session = _make_session()

        # Соединение свежее — внутри grace-period
        fresh_ts = datetime.now(UTC) - timedelta(minutes=5)
        conn = _make_synced_connection(
            conn_id=21,
            email="fresh@vpn",
            last_sync_at=fresh_ts,
            created_at=fresh_ts,
        )

        from app.services.sync_service import SyncService

        svc = SyncService.__new__(SyncService)
        svc.session = session
        svc._xui_service = MagicMock()

        xui_client = MagicMock()
        xui_client.get_clients = AsyncMock(return_value=[])

        server = _make_server()

        execute_results = [
            _make_scalars_result([]),
            _make_rows_result([]),
            _make_rows_result([]),
            _make_rows_result([]),
            _make_scalars_result([conn]),    # 2c: свежее synced-соединение
        ]
        session.execute = AsyncMock(side_effect=execute_results)

        notify_mock = AsyncMock()
        with patch(
            "app.services.notification_service.NotificationService.notify_admins_missing_on_panel",
            new=notify_mock,
        ):
            await svc._reconcile_xui_server(server, xui_client)

        # НЕ должно быть помечено
        assert conn.sync_status == "synced"
        session.delete.assert_not_called()
        notify_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_synced_present_on_panel_not_touched(self, mock_settings):
        """synced + email В снимке → не трогаем."""
        from unittest.mock import AsyncMock, MagicMock, patch

        session = _make_session()

        conn = _make_synced_connection(conn_id=22, email="present@vpn")

        from app.services.sync_service import SyncService

        svc = SyncService.__new__(SyncService)
        svc.session = session
        svc._xui_service = MagicMock()

        xui_client = MagicMock()
        # Снимок панели: present@vpn ПРИСУТСТВУЕТ
        xui_client.get_clients = AsyncMock(
            return_value=[{"email": "present@vpn", "subId": "", "inboundIds": []}]
        )

        server = _make_server()

        execute_results = [
            _make_scalars_result([]),
            _make_rows_result([]),
            _make_rows_result([]),
            _make_rows_result([]),
            _make_scalars_result([conn]),
        ]
        session.execute = AsyncMock(side_effect=execute_results)

        notify_mock = AsyncMock()
        with patch(
            "app.services.notification_service.NotificationService.notify_admins_missing_on_panel",
            new=notify_mock,
        ):
            await svc._reconcile_xui_server(server, xui_client)

        # Не трогаем
        assert conn.sync_status == "synced"
        session.delete.assert_not_called()
        notify_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_second_pass_error_absent_deleted(self, mock_settings):
        """Второй проход: соединение уже 'error' + email всё ещё отсутствует → step 2a удаляет из БД.

        Снимок НЕПУСТОЙ: другой клиент присутствует, removed@vpn — нет (реальный сценарий).
        """
        from unittest.mock import AsyncMock, MagicMock

        session = _make_session()

        # Соединение уже помечено как error (после первого прохода 2c)
        conn = _make_xui_connection(conn_id=30, email="removed@vpn", sync_status="error")

        from app.services.sync_service import SyncService

        svc = SyncService.__new__(SyncService)
        svc.session = session
        svc._xui_service = MagicMock()

        xui_client = MagicMock()
        # Снимок НЕПУСТОЙ: removed@vpn всё ещё отсутствует, но другой клиент есть
        xui_client.get_clients = AsyncMock(
            return_value=[{"email": "active@vpn", "subId": "", "inboundIds": []}]
        )

        server = _make_server()

        execute_results = [
            _make_scalars_result([conn]),    # 2a: находим error-соединение
            _make_rows_result([]),           # 2b
            _make_rows_result([]),           # 2b
            _make_rows_result([]),           # 2b
            _make_scalars_result([]),        # 2c: нет synced-соединений (уже в error)
        ]
        session.execute = AsyncMock(side_effect=execute_results)

        await svc._reconcile_xui_server(server, xui_client)

        # Шаг 2a удаляет из БД
        session.delete.assert_called_once_with(conn)

    @pytest.mark.asyncio
    async def test_get_clients_raises_nothing_marked(self, mock_settings):
        """get_clients() бросает → шаг 2c не вызывается, ничего не помечается, уведомлений нет."""
        from unittest.mock import AsyncMock, MagicMock, patch

        session = _make_session()

        from app.services.sync_service import SyncService

        svc = SyncService.__new__(SyncService)
        svc.session = session
        svc._xui_service = MagicMock()

        xui_client = MagicMock()
        xui_client.get_clients = AsyncMock(side_effect=Exception("timeout"))

        server = _make_server()
        session.execute = AsyncMock()

        notify_mock = AsyncMock()
        with patch(
            "app.services.notification_service.NotificationService.notify_admins_missing_on_panel",
            new=notify_mock,
        ):
            await svc._reconcile_xui_server(server, xui_client)

        session.delete.assert_not_called()
        session.execute.assert_not_called()
        notify_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_snapshot_skips_all_destructive_steps(self, mock_settings):
        """CRITICAL guard: get_clients() → [] (пустой снимок) → ни одно соединение не помечается
        и ничего не удаляется. Защищает от soft-fail панели (истёкший токен / rate-limit)."""
        from unittest.mock import AsyncMock, MagicMock, patch

        session = _make_session()

        # Живое synced-соединение (старше grace-period) — должно остаться нетронутым
        conn = _make_synced_connection(conn_id=40, email="live@vpn")

        # Error-соединение — тоже не должно быть удалено при пустом снимке
        error_conn = _make_xui_connection(conn_id=41, email="ghost@vpn", sync_status="error")

        from app.services.sync_service import SyncService

        svc = SyncService.__new__(SyncService)
        svc.session = session
        svc._xui_service = MagicMock()

        xui_client = MagicMock()
        # Пустой снимок — имитирует soft-fail панели (success:false → [])
        xui_client.get_clients = AsyncMock(return_value=[])
        xui_client.delete_client = AsyncMock()

        server = _make_server()
        session.execute = AsyncMock()

        notify_mock = AsyncMock()
        with patch(
            "app.services.notification_service.NotificationService.notify_admins_missing_on_panel",
            new=notify_mock,
        ):
            await svc._reconcile_xui_server(server, xui_client)

        # Ранний выход: session.execute не вызывается (нет деструктивных шагов)
        session.execute.assert_not_called()
        # Ни одно соединение не удалено из БД
        session.delete.assert_not_called()
        # Ни одно соединение на панели не удалено
        xui_client.delete_client.assert_not_called()
        # Уведомлений нет
        notify_mock.assert_not_awaited()
        # Статусы соединений не изменены
        assert conn.sync_status == "synced"
        assert error_conn.sync_status == "error"

    @pytest.mark.asyncio
    async def test_error_connection_self_heals_when_present_on_panel(self, mock_settings):
        """Self-heal: соединение со sync_status='error', email ПРИСУТСТВУЕТ в непустом снимке
        → восстанавливается в sync_status='synced', НЕ удаляется (шаг 2a present-error → synced)."""
        from unittest.mock import AsyncMock, MagicMock

        session = _make_session()

        # Соединение в статусе error (было помечено на прошлом проходе)
        conn = _make_xui_connection(conn_id=50, email="recovering@vpn", sync_status="error")

        from app.services.sync_service import SyncService

        svc = SyncService.__new__(SyncService)
        svc.session = session
        svc._xui_service = MagicMock()

        xui_client = MagicMock()
        # Непустой снимок: recovering@vpn ПРИСУТСТВУЕТ — клиент вернулся на панель
        xui_client.get_clients = AsyncMock(
            return_value=[
                {"email": "recovering@vpn", "subId": "", "inboundIds": []},
                {"email": "other@vpn", "subId": "", "inboundIds": []},
            ]
        )
        xui_client.delete_client = AsyncMock()

        server = _make_server()

        execute_results = [
            _make_scalars_result([conn]),   # 2a: находим error-соединение
            _make_rows_result([]),          # 2b: нет токенов
            _make_rows_result([]),          # 2b: нет xui inbounds
            _make_rows_result([]),          # 2b: нет пар
            _make_scalars_result([]),       # 2c: нет synced-соединений
        ]
        session.execute = AsyncMock(side_effect=execute_results)

        await svc._reconcile_xui_server(server, xui_client)

        # Восстановлено в synced — НЕ удалено
        assert conn.sync_status == "synced"
        session.delete.assert_not_called()
        xui_client.delete_client.assert_not_called()


# ---------------------------------------------------------------------------
# C5. Transaction boundary tests
# ---------------------------------------------------------------------------


class TestTransactionBoundaries:
    """C5: при исключении в sync_server → rollback, не commit."""

    @pytest.mark.asyncio
    async def test_exception_triggers_rollback_not_commit(self, mock_settings):
        """Исключение в _sync_server_inbounds → rollback вызван, commit НЕ вызван."""
        session = _make_session()

        from app.services.sync_service import SyncService

        svc = SyncService.__new__(SyncService)
        svc.session = session
        svc._is_running = False
        svc._xui_service = MagicMock()
        import asyncio
        svc._sync_lock = asyncio.Lock()

        server = _make_server()
        server.last_sync_at = None
        server.sync_status = "error"  # needs sync

        # Patch _save_server_error_status to a no-op so we don't need a full DB
        with (
            patch.object(svc, "_needs_sync", return_value=True),
            patch.object(
                svc,
                "_sync_server_inbounds",
                new=AsyncMock(side_effect=RuntimeError("boom")),
            ),
            patch.object(svc, "_save_server_error_status", new=AsyncMock()),
            patch("app.services.server_monitor.ServerMonitor.check_tcp", new=AsyncMock(return_value=True)),
        ):
            # Мокируем session.execute для запроса inbounds
            session.execute = AsyncMock(return_value=_make_scalars_result([]))

            result = await svc.sync_server(server, force=True)

        assert result is False
        session.rollback.assert_called_once()
        session.commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_success_triggers_single_commit_no_rollback(self, mock_settings):
        """Успешная синхронизация → ровно один commit, rollback не вызван."""
        session = _make_session()

        from app.services.sync_service import SyncService

        svc = SyncService.__new__(SyncService)
        svc.session = session
        svc._is_running = False
        svc._xui_service = MagicMock()
        svc._xui_service._get_client = AsyncMock(return_value=MagicMock())
        import asyncio
        svc._sync_lock = asyncio.Lock()

        server = _make_server()
        server.last_sync_at = None
        server.sync_status = "synced"

        reconcile_mock = AsyncMock()

        with (
            patch.object(svc, "_needs_sync", return_value=True),
            patch.object(svc, "_sync_server_inbounds", new=AsyncMock()),
            patch.object(svc, "_reconcile_xui_server", new=reconcile_mock),
            patch("app.services.server_monitor.ServerMonitor.check_tcp", new=AsyncMock(return_value=True)),
        ):
            session.execute = AsyncMock(return_value=_make_scalars_result([]))

            result = await svc.sync_server(server, force=True)

        assert result is True
        session.commit.assert_called_once()
        session.rollback.assert_not_called()

    @pytest.mark.asyncio
    async def test_error_status_saved_after_rollback(self, mock_settings):
        """При ошибке sync_server: rollback → _save_server_error_status вызван с нужными аргументами."""
        from app.xui_client import XUIError

        session = _make_session()

        from app.services.sync_service import SyncService

        svc = SyncService.__new__(SyncService)
        svc.session = session
        svc._is_running = False
        svc._xui_service = MagicMock()
        svc._xui_service._get_client = AsyncMock(return_value=MagicMock())
        import asyncio
        svc._sync_lock = asyncio.Lock()

        server = _make_server()
        server.last_sync_at = None
        server.sync_status = "synced"

        save_status_mock = AsyncMock()

        with (
            patch.object(svc, "_needs_sync", return_value=True),
            patch.object(
                svc,
                "_sync_server_inbounds",
                new=AsyncMock(side_effect=XUIError("panel error")),
            ),
            patch.object(svc, "_save_server_error_status", new=save_status_mock),
            patch("app.services.server_monitor.ServerMonitor.check_tcp", new=AsyncMock(return_value=True)),
        ):
            session.execute = AsyncMock(return_value=_make_scalars_result([]))

            result = await svc.sync_server(server, force=True)

        assert result is False
        session.rollback.assert_called_once()
        # _save_server_error_status должен быть вызван ПОСЛЕ rollback
        save_status_mock.assert_awaited_once()
        call_args = save_status_mock.await_args
        assert call_args.args[0] == server.id  # server_id
        assert call_args.args[1] == "error"    # sync_status


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------


def _make_scalars_result(objects: list) -> MagicMock:
    """Return a mock that mimics session.execute().scalars().all()."""
    result = MagicMock()
    scalars_mock = MagicMock()
    scalars_mock.all = MagicMock(return_value=objects)
    result.scalars = MagicMock(return_value=scalars_mock)
    return result


def _make_rows_result(rows: list) -> MagicMock:
    """Return a mock that mimics session.execute().all() (for row queries)."""
    result = MagicMock()
    result.all = MagicMock(return_value=rows)
    result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=rows)))
    return result
