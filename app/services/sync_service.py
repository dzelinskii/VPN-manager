"""Service for synchronizing data between bot database and XUI panels."""

import asyncio
import contextlib
import json
import uuid
from datetime import UTC, datetime, timedelta

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.models import (
    Inbound,
    InboundConnection,
    Server,
    Subscription,
)
from app.services.protocol_sync import for_inbound
from app.services.xui_service import XUIService
from app.xui_client import XUIConnectionError, XUIError
from app.xui_client.models import ensure_settings_dict

# Глобальная блокировка для предотвращения конфликтов между всеми экземплярами SyncService
_global_sync_lock = asyncio.Lock()

# Зомби-клиенты моложе этого порога НЕ удаляются автоматически —
# защита от гонки с незакоммиченной сагой add_inbound_to_subscription.
ZOMBIE_GRACE_PERIOD = timedelta(minutes=15)


class SyncService:
    """Service for synchronizing data between database and XUI panels."""

    SYNC_INTERVAL = timedelta(minutes=5)  # 5 минут между синхронизациями

    def __init__(self, session: AsyncSession) -> None:
        """Initialize sync service.

        Args:
            session: Async database session
        """
        self.session = session
        self._is_running = False
        # Используем глобальную блокировку вместо локальной
        self._sync_lock = _global_sync_lock

        # Initialize centralized XUIService for connection pooling and proper cleanup
        self._xui_service = XUIService(session)

    # === CORE METHODS ===

    async def start_background_sync(self) -> None:
        """Запустить фоновую синхронизацию."""
        if self._is_running:
            logger.warning("Фоновая синхронизация уже запущена")
            return

        self._is_running = True
        logger.info("Запуск фоновой синхронизации данных")

        while self._is_running:
            try:
                await self._sync_cycle(force=False)
                # Wait for SYNC_INTERVAL (5 minutes) between cycles
                logger.debug("Ожидание следующего цикла синхронизации...")
                await asyncio.sleep(self.SYNC_INTERVAL.total_seconds())
            except Exception as e:
                logger.error("Ошибка цикла синхронизации: {} - {}", type(e).__name__, str(e), exc_info=True)
                await asyncio.sleep(60)  # 1 минута при ошибке

        logger.info("Фоновая синхронизация остановлена")

    async def stop_background_sync(self) -> None:
        """Остановить фоновую синхронизацию."""
        self._is_running = False
        logger.info("Остановка фоновой синхронизации")

    async def close_xui_clients(self) -> None:
        """Закрыть все XUI клиенты для предотвращения утечек ресурсов."""
        if hasattr(self, "_xui_service") and self._xui_service:
            await self._xui_service.close_all_clients()
        logger.debug("XUI clients closed")

    async def _sync_cycle(self, force: bool = False) -> dict:
        """Один цикл синхронизации.

        Args:
            force: Принудительная синхронизация (для ручной синхронизации)

        Returns:
            Словарь с результатами синхронизации
        """
        # Проверить, есть ли другая активная синхронизация
        if self._sync_lock.locked():
            logger.debug("Пропуск цикла синхронизации — другая синхронизация уже выполняется")
            return {"servers": 0, "clients": 0}

        async with self._sync_lock:
            cycle_id = uuid.uuid4().hex[:8]
            with logger.contextualize(cycle=cycle_id):
                start_time = datetime.now(UTC)
                logger.info("Начало цикла синхронизации (force={})", force)

                try:
                    # 1. Синхронизировать сервера и inbounds (включая клиентов)
                    servers_synced = await self.sync_all_servers(force=force)

                    # 2. Синхронизировать клиентов (только если sync_server не сделал этого)
                    # sync_server уже синхронизирует клиентов, поэтому вызов sync_all_clients будет дублировать
                    # Поэтому мы не вызываем sync_all_clients здесь

                    # 3. Проверить целостность подключений
                    integrity_ok = await self.verify_connections_integrity()

                    # 4. Логировать результаты
                    duration = (datetime.now(UTC) - start_time).total_seconds()
                    logger.info(
                        "Цикл синхронизации завершён за {:.2f}s. Серверов: {}, целостность: {}",
                        duration, servers_synced, integrity_ok,
                    )

                    return {"servers": servers_synced}

                except Exception as e:
                    logger.error("Ошибка в цикле синхронизации: {} - {}", type(e).__name__, str(e), exc_info=True)
                    return {"servers": 0, "error": f"{type(e).__name__}: {str(e)}"}

    # === SERVER SYNC ===

    async def sync_all_servers(self, force: bool = False) -> int:
        """Синхронизировать все активные сервера.

        Args:
            force: Принудительная синхронизация

        Returns:
            Количество синхронизированных серверов
        """
        # Берём только ID активных серверов и перечитываем каждый сервер свежим
        # внутри цикла. Иначе rollback() при ошибке одного сервера обесценивает
        # (expire) ORM-объекты остальных в общей сессии, и обращение к ним в
        # async вызывает MissingGreenlet, роняя весь цикл.
        ids_result = await self.session.execute(select(Server.id).where(Server.is_active))
        server_ids = ids_result.scalars().all()

        logger.info("Активных серверов: {} (force={})", len(server_ids), force)

        synced_count = 0
        for i, server_id in enumerate(server_ids, 1):
            with logger.contextualize(server=server_id):
                server = await self._load_server_for_sync(server_id)
                if server is None:
                    continue
                server_name = server.name
                try:
                    logger.debug("Сервер {}/{}: {}", i, len(server_ids), server_name)
                    synced = await self.sync_server(server, force=force)
                    if synced:
                        synced_count += 1
                        logger.info("Сервер {} синхронизирован", server_name)
                    else:
                        logger.debug("Сервер {} пропущен (актуален или ошибка)", server_name)
                except Exception as e:
                    logger.error(
                        "Ошибка синхронизации сервера {}: {} - {}", server_id, type(e).__name__, str(e),
                        exc_info=True,
                    )

        logger.info("Завершено: {}/{} серверов синхронизировано", synced_count, len(server_ids))
        return synced_count

    async def _load_server_for_sync(self, server_id: int) -> Server | None:
        """Свежо загрузить сервер с eager-связями для синхронизации.

        Перечитывание на каждой итерации цикла гарантирует, что rollback()
        предыдущего сервера не оставит expired-объект (см. sync_all_servers).
        """
        result = await self.session.execute(
            select(Server)
            .where(Server.id == server_id)
            .options(
                selectinload(Server.xui_panel),
                selectinload(Server.awg_service),
                selectinload(Server.mtproxy_service),
                selectinload(Server.inbounds),
            )
        )
        return result.scalar_one_or_none()

    async def sync_server(self, server: Server, force: bool = False) -> bool:
        """Синхронизировать отдельный сервер.

        Транзакционные границы (C5):
        - Один commit() ПОСЛЕ полной успешной синхронизации сервера.
        - При исключении — rollback(), лог, возврат False (одна ошибка не валит весь цикл).
        - Нет finally-commit (не фиксируем частичное состояние при ошибке).

        Args:
            server: Server model
            force: Принудительная синхронизация

        Returns:
            True если успешно, False если ошибка
        """
        # server_id фиксируем ДО возможного rollback: после rollback ORM-объект
        # становится expired, и обращение к server.id в async-обработчиках ошибок
        # вызывало бы MissingGreenlet (неявное IO вне greenlet).
        server_id = server.id
        try:
            # Проверить, нужна ли синхронизация
            if not force and not self._needs_sync(server):
                logger.debug("Сервер {} в актуальном состоянии", server.name)
                return False

            logger.info("Синхронизация сервера {}", server.name)

            # TCP-проба SSH-порта сервера для обновления статуса online
            if server.ip_address:
                from app.services.server_monitor import ServerMonitor
                host = server.ip_address
                if host.startswith("http://"):
                    host = host[7:]
                elif host.startswith("https://"):
                    host = host[8:]
                if ":" in host:
                    host = host.split(":")[0]
                probe_port = server.ssh_port or 22
                is_online = await ServerMonitor.check_tcp(host, probe_port)
                server.is_online = is_online
                logger.debug(
                    "TCP-проба {}:{}: {}",
                    host, probe_port, "доступен" if is_online else "недоступен",
                )

            xui_client = None
            if server.is_online and server.xui_panel and server.xui_panel.url and server.xui_panel.username:
                # Получить XUI клиент
                xui_client = await self._xui_service._get_client(server)
                # Синхронизировать inbounds
                await self._sync_server_inbounds(server, xui_client)
            else:
                logger.debug("Сервер {} не имеет настроенной XUI-панели, пропуск XUI inbounds", server.name)

            # Синхронизация клиентов для всех inbounds этого сервера
            from sqlalchemy import select
            from sqlalchemy.orm import with_polymorphic

            conn_poly = with_polymorphic(InboundConnection, "*")
            inbound_poly = with_polymorphic(Inbound, "*")

            inbounds_result = await self.session.execute(
                select(inbound_poly).where(
                    inbound_poly.server_id == server.id, inbound_poly.is_active
                ).options(
                    selectinload(inbound_poly.client_connections.of_type(conn_poly))
                    .selectinload(conn_poly.subscription)
                    .selectinload(Subscription.client)
                )
            )
            inbounds = inbounds_result.scalars().all()

            clients_synced = 0
            logger.debug("Активных inbounds на сервере {}: {}", server.name, len(inbounds))
            for inbound in inbounds:
                try:
                    logger.debug("Синхронизация клиентов inbound {} ({})", inbound.id, inbound.remark)
                    synced = await self._sync_inbound_clients(inbound)
                    clients_synced += synced
                    logger.debug("Inbound {}: {} клиентов синхронизировано", inbound.id, synced)
                except Exception as e:
                    logger.error(
                        "Ошибка синхронизации клиентов inbound {}: {} - {}", inbound.id, type(e).__name__, str(e),
                        exc_info=True,
                    )

            # Реконсиляция зомби/фантомов для XUI-серверов
            if server.is_online and server.xui_panel and xui_client is not None:
                await self._reconcile_xui_server(server, xui_client)

            # Обновить статус синхронизации
            server.last_sync_at = datetime.now(UTC)
            server.sync_status = "synced"
            server.sync_error = None

            await self.session.flush()
            # Единственный commit после полной успешной синхронизации сервера
            await self.session.commit()
            logger.info("Сервер {} синхронизирован, клиентов: {}", server_id, clients_synced)
            return True

        except XUIConnectionError as e:
            new_status = "offline"
            new_error = f"Connection failed: {str(e)}"
            logger.warning("Сервер недоступен (XUI): {}", e)
            await self.session.rollback()
            await self._save_server_error_status(server_id, new_status, new_error)
            return False

        except XUIError as e:
            new_status = "error"
            new_error = str(e)
            logger.warning("Ошибка XUI-сервера: {}", e)
            await self.session.rollback()
            await self._save_server_error_status(server_id, new_status, new_error)
            return False

        except Exception as e:
            # Check for Amnezia errors without importing at top level
            if type(e).__name__ == "AmneziaConnectionError":
                new_status = "offline"
                new_error = f"Connection failed: {str(e)}"
                logger.warning("Сервер недоступен (Amnezia): {}", e)
                await self.session.rollback()
                await self._save_server_error_status(server_id, new_status, new_error)
                return False
            elif type(e).__name__ == "AmneziaError":
                new_status = "error"
                new_error = str(e)
                logger.warning("Ошибка Amnezia-сервера: {}", e)
                await self.session.rollback()
                await self._save_server_error_status(server_id, new_status, new_error)
                return False

            logger.error(
                "Неожиданная ошибка при синхронизации сервера: {} - {}", type(e).__name__, str(e),
                exc_info=True,
            )
            await self.session.rollback()
            await self._save_server_error_status(server_id, "error", f"{type(e).__name__}: {str(e)}")
            return False

        # Don't close clients - keep them cached for reuse
        # finally:
        #     if xui_service:
        #         await xui_service.close_all_clients()

    async def _save_server_error_status(
        self,
        server_id: int,
        sync_status: str,
        sync_error: str,
    ) -> None:
        """Сохранить диагностический статус ошибки сервера в отдельной транзакции.

        Вызывается ПОСЛЕ rollback() бизнес-транзакции, чтобы статус попал в БД
        независимо от откатанных бизнес-данных.

        Args:
            server_id: ID сервера
            sync_status: Новый статус ('error', 'offline')
            sync_error: Текст ошибки
        """
        try:
            srv = await self.session.get(Server, server_id)
            if srv is not None:
                srv.sync_status = sync_status
                srv.sync_error = sync_error
                srv.last_sync_at = datetime.now(UTC)
                await self.session.commit()
                logger.debug("Статус ошибки сервера {} сохранён: {}", server_id, sync_status)
        except Exception as status_err:
            logger.warning("Не удалось сохранить статус ошибки сервера {}: {}", server_id, status_err)
            with contextlib.suppress(Exception):
                await self.session.rollback()

    async def sync_all_clients(self) -> int:
        """Синхронизировать всех клиентов на всех активных inbounds.

        Returns:
            Количество синхронизированных клиентов
        """
        from sqlalchemy import select
        from sqlalchemy.orm import with_polymorphic

        inbound_poly = with_polymorphic(Inbound, "*")
        conn_poly_load = with_polymorphic(InboundConnection, "*")

        # Получить все активные inbounds с серверами
        result = await self.session.execute(
            select(inbound_poly)
            .where(inbound_poly.is_active)
            .options(
                selectinload(inbound_poly.server).selectinload(Server.xui_panel),
                selectinload(inbound_poly.client_connections.of_type(conn_poly_load))
                .selectinload(conn_poly_load.subscription)
                .selectinload(Subscription.client),
            )
        )
        inbounds = result.scalars().all()

        total_synced = 0

        try:
            for inbound in inbounds:
                try:
                    synced = await self._sync_inbound_clients(inbound)
                    total_synced += synced

                    # Фиксируем каждого inbound
                    await self.session.commit()

                except Exception as e:
                    logger.error(
                        "Ошибка синхронизации клиентов inbound {}: {} - {}", inbound.id, type(e).__name__, str(e),
                        exc_info=True,
                    )
                    await self.session.commit()

        except Exception as e:
            logger.error("Ошибка в sync_all_clients: {} - {}", type(e).__name__, str(e), exc_info=True)

        # Don't close clients - keep them cached for reuse
        # finally:
        #     if xui_service:
        #         await xui_service.close_all_clients()

        logger.info("Синхронизировано клиентов: {}", total_synced)
        return total_synced

    async def sync_server_clients(self, server_id: int) -> int:
        """Синхронизировать клиентов для конкретного сервера.

        Args:
            server_id: ID сервера

        Returns:
            Количество синхронизированных клиентов
        """
        from sqlalchemy import select
        from sqlalchemy.orm import with_polymorphic

        # Получить сервер
        server = await self.session.get(
            Server,
            server_id,
            options=[
                selectinload(Server.xui_panel),
                selectinload(Server.awg_service),
                selectinload(Server.mtproxy_service),
                selectinload(Server.inbounds),
            ],
        )
        if not server:
            logger.warning("Сервер {} не найден", server_id)
            return 0

        inbound_poly = with_polymorphic(Inbound, "*")
        conn_poly_load = with_polymorphic(InboundConnection, "*")

        result = await self.session.execute(
            select(inbound_poly).where(inbound_poly.server_id == server_id, inbound_poly.is_active)
            .options(
                selectinload(inbound_poly.client_connections.of_type(conn_poly_load))
                .selectinload(conn_poly_load.subscription)
                .selectinload(Subscription.client),
            )
        )
        inbounds = result.scalars().all()

        total_synced = 0

        try:
            for inbound in inbounds:
                try:
                    synced = await self._sync_inbound_clients(inbound)
                    total_synced += synced

                    # Фиксируем каждого inbound
                    await self.session.commit()
                except Exception as e:
                    logger.error(
                        "Ошибка синхронизации клиентов inbound {}: {} - {}", inbound.id, type(e).__name__, str(e),
                        exc_info=True,
                    )
                    await self.session.commit()

        except Exception as e:
            logger.error("Ошибка в sync_server_clients: {}", e, exc_info=True)

        # Don't close clients - keep them cached for reuse
        # finally:
        #     if xui_service:
        #         await xui_service.close_all_clients()

        logger.info("Синхронизировано клиентов на сервере {}: {}", server_id, total_synced)
        return total_synced

    def _needs_sync(self, model: object) -> bool:
        """Проверить, нужна ли синхронизация.

        Args:
            model: Model with sync fields

        Returns:
            True если нужна синхронизация
        """
        # Используем __dict__, чтобы не вызывать lazy loading (MissingGreenlet в SQLAlchemy)
        model_dict = getattr(model, "__dict__", {})

        sync_status = model_dict.get("sync_status")
        if sync_status == "offline" or sync_status == "error":
            return True  # Попробовать снова

        if "last_sync_at" in model_dict:
            last_sync_at = model_dict["last_sync_at"]
            if last_sync_at is None:
                return True  # Никогда не синхронизировали

            # Если прошло больше интервала (с учетом timezone-aware и timezone-naive)
            now = datetime.now(UTC)

            # Если last_sync не имеет timezone, добавим ему UTC timezone
            if last_sync_at.tzinfo is None:
                last_sync_at = last_sync_at.replace(tzinfo=UTC)

            if now - last_sync_at > self.SYNC_INTERVAL:
                return True
        else:
            # Если поля нет даже в __dict__ (например, не загружено), считаем что нужна синхронизация
            # Но для безопасности лучше просто вернуть True
            return True

        return False

    async def _sync_server_inbounds(self, server: Server, xui_client: object) -> None:
        """Синхронизировать inbounds сервера.

        Args:
            server: Server model
            xui_client: XUI client instance
        """
        # Получить inbounds из XUI
        xui_inbounds = await xui_client.get_inbounds()

        from sqlalchemy.orm import with_polymorphic

        inbound_poly = with_polymorphic(Inbound, "*")

        # Сопоставить с существующими
        existing_inbounds = await self.session.execute(
            select(inbound_poly)
            .where(inbound_poly.server_id == server.id)
            .options(selectinload(inbound_poly.server))
        )
        existing_map = {
            getattr(ib, "xui_id", None): ib
            for ib in existing_inbounds.scalars().all()
            if getattr(ib, "xui_id", None) is not None
        }

        # Обновить или создать inbounds
        for xui_ib in xui_inbounds:
            xui_id = xui_ib.id

            # Parse settings JSON to get client count
            settings_dict = ensure_settings_dict(xui_ib.settings)
            client_count = len(settings_dict.get("clients", []))

            settings_str = xui_ib.settings if isinstance(xui_ib.settings, str) else json.dumps(xui_ib.settings or {})

            if xui_id in existing_map:
                db_ib = existing_map[xui_id]
                if settings_str != db_ib.settings_json or xui_ib.remark != db_ib.remark:
                    db_ib.settings_json = settings_str
                    db_ib.remark = xui_ib.remark
                    db_ib.client_count = client_count
                    db_ib.updated_at = datetime.now(UTC)
                    db_ib.sync_status = "synced"
                    db_ib.last_sync_at = datetime.now(UTC)
                    logger.debug("Inbound {} обновлён из XUI", db_ib.id)
                else:
                    logger.debug("Inbound {} актуален", db_ib.id)
                    db_ib.sync_status = "synced"
                    db_ib.last_sync_at = datetime.now(UTC)
            else:
                # Создать новый inbound
                from app.database.models import XUIInbound
                new_ib = XUIInbound(
                    server_id=server.id,
                    xui_id=xui_id,
                    remark=xui_ib.remark,
                    protocol=xui_ib.protocol,
                    port=xui_ib.port,
                    settings_json=settings_str,
                    client_count=client_count,
                    is_active=True,
                    sync_status="synced",
                    last_sync_at=datetime.now(UTC),
                )
                self.session.add(new_ib)
                logger.info("Создан новый inbound из XUI: remark={}", xui_ib.remark)

        await self.session.flush()

    async def _reconcile_xui_server(self, server: Server, xui_client: object) -> None:
        """Реконсиляция зомби/фантомов для одного XUI-сервера.

        Единый снимок get_clients():
        - Если get_clients() упал — весь шаг реконсиляции пропускается (ничего
          не удаляется).
        - Если get_clients() вернул пустой список ([] — неотличимо от soft-fail
          панели при истёкшем токене / rate-limit) — реконсиляция тоже пропускается.
          При реально пустой панели нечего удалять; пропуск безопасен.
        - Один снимок используется и для проверки фантомов, и для зомби.

        2a. Фантомы БД: InboundConnection с sync_status='error'
            - Если email клиента НЕ в снимке get_clients() → фантом, удаляем из БД.
            - Если email есть в снимке → восстановить sync_status='synced'.

        2b. XUI bot-зомби (авто-удаление только при надёжной bot-подписи):
            - Панельный клиент с непустым subId, совпадающим с subscription_token
              СУЩЕСТВУЮЩЕЙ подписки в БД, но без InboundConnection для этого
              inbound → наш орфан → удаляем с панели (высокая уверенность).
            - Grace-period: зомби с createdAt моложе ZOMBIE_GRACE_PERIOD НЕ удаляется
              в этом цикле (защита от гонки с незакоммиченной сагой add).
            - Клиент без createdAt или createdAt==0 → не удалять (безопаснее пропустить).
            - subId не совпадает ни с одной подпиской → НЕ удалять, только warning.
            - Клиент без subId → не трогать (debug-лог).
            - Пустой email → пропустить.

        2c. Вручную удалённые на панели (mirror-step):
            - XUIInboundConnection этого сервера со sync_status='synced', чей email
              отсутствует в снимке get_clients() И старше ZOMBIE_GRACE_PERIOD
              (по last_sync_at если есть, иначе created_at) → помечаем sync_status='error'.
              НЕ удаляем в этом проходе — удалит шаг 2a на следующем проходе.
            - Свежие соединения (внутри grace-period) не трогаем.
            - Если что-то помечено — отправляем сводное уведомление администраторам.

        AWG/MTProxy: нет надёжной enumeration всех клиентов — только лог.
        Автоудаление не производится.

        Args:
            server: Server model
            xui_client: Подключённый XUIClient для этого сервера
        """
        from uuid import uuid4

        from sqlalchemy import select
        from sqlalchemy.orm import with_polymorphic

        from app.config import get_settings
        from app.database.models import Subscription
        from app.database.models.inbound import XUIInbound
        from app.database.models.inbound_connection import XUIInboundConnection
        from app.services.divergence_service import (
            KIND_EXTRA,
            KIND_MISSING,
            DivergenceFinding,
            DivergenceService,
        )

        logger.debug("Начало реконсиляции для сервера {}", server.name)

        # -----------------------------------------------------------------------
        # Единый надёжный снимок панели.
        # Если get_clients() бросил исключение — весь шаг реконсиляции пропускается.
        # -----------------------------------------------------------------------
        try:
            panel_clients = await xui_client.get_clients()
        except Exception as e:
            logger.warning(
                "Не удалось получить список клиентов панели сервера {}, реконсиляция пропущена: {}",
                server.name, e,
            )
            return

        if panel_clients is None:
            logger.warning(
                "get_clients() вернул None для сервера {}, реконсиляция пропущена",
                server.name,
            )
            return

        # SAFETY GUARD: пустой снимок — пропускаем все деструктивные шаги.
        # Пустой список неотличим от soft-fail панели (истёкший токен / rate-limit
        # возвращает success:false → get_clients() возвращает []).
        # При настоящей пустой панели нечего удалять — пропуск безопасен.
        if not panel_clients:
            logger.warning(
                "get_clients() вернул пустой снимок для сервера {}, реконсиляция пропущена (возможен сбой API)",
                server.name,
            )
            return

        # Набор email-адресов, присутствующих на панели (для проверки фантомов)
        panel_emails: set[str] = {
            (c.get("email") or "").lower()
            for c in panel_clients
            if c.get("email")
        }

        # -----------------------------------------------------------------------
        # 2a. Фантомы БД: XUIInboundConnection с sync_status='error'
        # Используем снимок get_clients() — НЕ отдельный get_client_traffic().
        # -----------------------------------------------------------------------
        conn_poly = with_polymorphic(XUIInboundConnection, "*")
        phantom_result = await self.session.execute(
            select(conn_poly)
            .join(XUIInbound, conn_poly.inbound_id == XUIInbound.id)
            .where(
                XUIInbound.server_id == server.id,
                conn_poly.sync_status == "error",
            )
        )
        error_connections = phantom_result.scalars().all()

        logger.debug(
            "Соединений со статусом 'error' для сервера {}: {}", server.name, len(error_connections)
        )

        # Детект (без действий): лечим присутствующих, собираем отсутствующих.
        heal_conns: list = []  # error, но клиент есть на панели → synced (любой режим)
        missing_from_error: list = []  # error, клиента нет на панели
        for conn in error_connections:
            c_email = getattr(conn, "email", None)
            if not c_email:
                logger.debug("Соединение {} без email, пропуск", conn.id)
                continue
            if c_email.lower() in panel_emails:
                heal_conns.append(conn)
            else:
                missing_from_error.append(conn)

        # -----------------------------------------------------------------------
        # 2b. XUI bot-зомби: панельные клиенты без соответствующей InboundConnection
        # -----------------------------------------------------------------------
        # Загружаем все subscription_token из БД (для быстрого поиска)
        sub_result = await self.session.execute(
            select(Subscription.subscription_token, Subscription.id)
        )
        token_to_sub_id: dict[str, int] = {row[0]: row[1] for row in sub_result.all()}

        # Загружаем все XUIInbound для этого сервера: xui_id → inbound.id
        xui_inbound_result = await self.session.execute(
            select(XUIInbound.xui_id, XUIInbound.id).where(XUIInbound.server_id == server.id)
        )
        xui_id_to_inbound_id: dict[int, int] = {row[0]: row[1] for row in xui_inbound_result.all()}

        # Загружаем существующие connections для быстрой проверки (subscription_id, inbound_id)
        conn_result = await self.session.execute(
            select(XUIInboundConnection.subscription_id, XUIInboundConnection.inbound_id)
            .join(XUIInbound, XUIInboundConnection.inbound_id == XUIInbound.id)
            .where(XUIInbound.server_id == server.id)
        )
        existing_pairs: set[tuple[int, int]] = set(conn_result.all())

        # Обратные карты для governed-деталей (adopt/restore).
        sub_id_to_token: dict[int, str] = {v: k for k, v in token_to_sub_id.items()}
        inbound_id_to_xui: dict[int, int] = {v: k for k, v in xui_id_to_inbound_id.items()}

        now_ms = datetime.now(UTC).timestamp() * 1000
        grace_ms = ZOMBIE_GRACE_PERIOD.total_seconds() * 1000

        # Детект (без действий): «лишнее на панели». Все safety-guard'ы сохранены.
        extra_items: list[dict] = []
        for panel_client in panel_clients:
            sub_id_field = panel_client.get("subId", "") or ""
            email = panel_client.get("email", "") or ""
            inbound_ids_on_panel: list[int] = panel_client.get("inboundIds") or []

            if not sub_id_field:
                # Нет subId → не бот-клиент, не трогаем
                logger.debug("Клиент '{}' без subId — пропуск (не бот-клиент)", email)
                continue

            if sub_id_field not in token_to_sub_id:
                # subId не совпадает ни с одной подпиской в БД — НЕ трогаем (ручной/чужой)
                logger.warning(
                    "Клиент '{}' (subId={!r}) на сервере {}: subId не совпадает ни с одной подпиской в БД, оставляем",
                    email, sub_id_field, server.name,
                )
                continue

            subscription_id = token_to_sub_id[sub_id_field]

            # Grace-period по createdAt (epoch ms): отсутствует/0/моложе порога → пропуск.
            created_at_ms = panel_client.get("createdAt") or 0
            if not created_at_ms:
                logger.debug(
                    "Клиент '{}' (subId={!r}) без createdAt — пропуск", email, sub_id_field
                )
                continue
            if (now_ms - created_at_ms) < grace_ms:
                logger.debug("Клиент '{}' моложе grace-period — пропуск", email)
                continue

            # Делим inbound'ы на валидные (есть InboundConnection) и осиротевшие.
            orphan_xui_ids: list[int] = []
            has_valid = False
            for xui_inbound_id in inbound_ids_on_panel:
                inbound_db_id = xui_id_to_inbound_id.get(xui_inbound_id)
                if inbound_db_id is None:
                    continue
                if (subscription_id, inbound_db_id) in existing_pairs:
                    has_valid = True
                else:
                    orphan_xui_ids.append(xui_inbound_id)

            if not orphan_xui_ids or not email:
                continue

            extra_items.append(
                {
                    "email": email,
                    "subscription_id": subscription_id,
                    "subscription_token": sub_id_field,
                    "orphan_xui_ids": orphan_xui_ids,
                    "has_valid": has_valid,
                    "panel_client": panel_client,
                }
            )

        # -----------------------------------------------------------------------
        # 2c. Вручную удалённые на панели: synced-соединения, отсутствующие в снимке.
        # Помечаем sync_status='error' (не удаляем — удалит шаг 2a на след. проходе).
        # Grace-period: возраст по last_sync_at (если задан), иначе по created_at.
        # -----------------------------------------------------------------------
        now_utc = datetime.now(UTC)
        grace = ZOMBIE_GRACE_PERIOD

        synced_result = await self.session.execute(
            select(conn_poly)
            .join(XUIInbound, conn_poly.inbound_id == XUIInbound.id)
            .where(
                XUIInbound.server_id == server.id,
                conn_poly.sync_status == "synced",
            )
        )
        synced_connections = synced_result.scalars().all()

        # Детект (без действий): synced-соединения, пропавшие с панели и старше grace.
        missing_from_synced: list[tuple] = []
        for conn in synced_connections:
            c_email = getattr(conn, "email", None)
            if not c_email or c_email.lower() in panel_emails:
                continue
            ref_ts = getattr(conn, "last_sync_at", None) or getattr(conn, "created_at", None)
            if ref_ts is None:
                logger.debug("Соединение {} (email={}) без временной метки — пропуск", conn.id, c_email)
                continue
            if ref_ts.tzinfo is None:
                ref_ts = ref_ts.replace(tzinfo=UTC)
            if (now_utc - ref_ts) < grace:
                logger.debug("Соединение {} (email={}) моложе grace-period — пропуск", conn.id, c_email)
                continue
            user_label = "—"
            try:
                sub = getattr(conn, "subscription", None)
                if sub is not None:
                    client = getattr(sub, "client", None)
                    if client is not None:
                        user_label = getattr(client, "name", None) or str(
                            getattr(client, "telegram_id", "—")
                        )
            except Exception:
                pass
            missing_from_synced.append((conn, user_label))

        # ============ РЕЖИМ ОБРАБОТКИ РАСХОЖДЕНИЙ ============
        settings = get_settings()
        mode = getattr(settings, "reconcile_mode", "ask") or "ask"
        threshold = int(getattr(settings, "reconcile_mass_threshold", 5) or 5)
        divergence_count = (
            len(missing_from_error) + len(missing_from_synced) + len(extra_items)
        )
        # Массовое расхождение — почти всегда системный сбой (откат/потеря панели).
        # Принудительно спрашиваем, даже если стоит auto: это предохранитель от
        # автоматического сноса десятков живых клиентов.
        effective_mode = "ask" if divergence_count > threshold else mode

        # Лечение присутствующих на панели — не деструктивно, выполняется всегда.
        for conn in heal_conns:
            logger.info(
                "Соединение {} (email={}) найдено на панели, восстановление sync_status='synced'",
                conn.id, getattr(conn, "email", None),
            )
            conn.sync_status = "synced"
        await self.session.flush()

        if effective_mode == "auto":
            await self._reconcile_apply_auto(
                server, xui_client, missing_from_error, missing_from_synced, extra_items
            )
            return

        # --- governed: ask / report — деструктив не выполняется ---
        findings: list[DivergenceFinding] = []
        present_keys: set[tuple[str, str]] = set()

        # missing: группируем соединения по email
        missing_by_email: dict[str, list] = {}
        for conn in missing_from_error:
            missing_by_email.setdefault(conn.email, []).append(conn)
        for conn, _ in missing_from_synced:
            missing_by_email.setdefault(conn.email, []).append(conn)

        for email, conns in missing_by_email.items():
            inbound_db_ids = [c.inbound_id for c in conns]
            sub_id = conns[0].subscription_id
            inbound_xui_ids = [
                inbound_id_to_xui[i] for i in inbound_db_ids if i in inbound_id_to_xui
            ]
            exp = getattr(conns[0], "expiry_date", None)
            expiry_ms = None
            if exp is not None:
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=UTC)
                expiry_ms = int(exp.timestamp() * 1000)
            findings.append(
                DivergenceFinding(
                    server_id=server.id,
                    kind=KIND_MISSING,
                    email=email,
                    subscription_id=sub_id,
                    details={
                        "inbound_db_ids": inbound_db_ids,
                        "inbound_xui_ids": inbound_xui_ids,
                        "uuid": getattr(conns[0], "uuid", None),
                        "subscription_token": sub_id_to_token.get(sub_id),
                        "total_gb": int(getattr(conns[0], "total_gb", 0) or 0),
                        "expiry_ms": expiry_ms,
                        "enable": bool(getattr(conns[0], "is_enabled", True)),
                    },
                )
            )
            present_keys.add((KIND_MISSING, email))

        # extra: панельные осиротевшие привязки/зомби
        for item in extra_items:
            pc = item["panel_client"]
            orphan_db_ids = [
                xui_id_to_inbound_id[x]
                for x in item["orphan_xui_ids"]
                if x in xui_id_to_inbound_id
            ]
            findings.append(
                DivergenceFinding(
                    server_id=server.id,
                    kind=KIND_EXTRA,
                    email=item["email"],
                    subscription_id=item["subscription_id"],
                    details={
                        "orphan_xui_ids": item["orphan_xui_ids"],
                        "has_valid": item["has_valid"],
                        "inbound_db_ids": orphan_db_ids,
                        "uuid": pc.get("uuid") or pc.get("id"),
                        "total_gb": int(pc.get("totalGB") or 0) // (1024**3),
                        "expiry_ms": int(pc.get("expiryTime") or 0),
                        "enable": bool(pc.get("enable", True)),
                        "subscription_token": item["subscription_token"],
                        "panel_payload": pc,
                    },
                )
            )
            present_keys.add((KIND_EXTRA, item["email"]))

        if effective_mode == "report":
            logger.info(
                "Сервер {}: режим report — расхождений {}, только уведомление",
                server.name, divergence_count,
            )
            await self._notify_divergences_report(server, findings)
            return

        # effective_mode == "ask" (в т.ч. форс при массовом сбое)
        svc = DivergenceService(self.session)
        batch_id = uuid4().hex[:8]
        created = await svc.record_findings(findings, batch_id)
        await svc.mark_obsolete(server.id, present_keys)
        await self.session.flush()
        logger.info(
            "Сервер {}: режим ask — расхождений {}, новых pending {}{}",
            server.name, divergence_count, len(created),
            " (массовый сбой!)" if divergence_count > threshold else "",
        )
        if created:
            await self._notify_divergences_ask(
                server, batch_id, created, divergence_count > threshold
            )

    async def _reconcile_apply_auto(
        self,
        server: Server,
        xui_client: object,
        missing_from_error: list,
        missing_from_synced: list,
        extra_items: list[dict],
    ) -> None:
        """Режим auto: выполнить деструктив немедленно (прежнее поведение).

        - missing из error-соединений → удалить строку БД;
        - missing из synced-соединений → пометить error + уведомить (удалит 2a на след. проходе);
        - extra → detach осиротевшей привязки или delete зомби целиком.
        """
        for conn in missing_from_error:
            logger.info(
                "Фантом: соединение {} (email={}) отсутствует на панели, удаление из БД",
                conn.id, getattr(conn, "email", None),
            )
            self.session.delete(conn)

        orphans_deleted = 0
        orphans_detached = 0
        for item in extra_items:
            email = item["email"]
            orphan = item["orphan_xui_ids"]
            if item["has_valid"]:
                logger.info(
                    "XUI-фантом-привязка: клиент '{}' сервера {}: inbound'ы {} без InboundConnection — detach",
                    email, server.name, orphan,
                )
                try:
                    await xui_client.detach_client(email, orphan)
                    orphans_detached += 1
                except Exception as e:
                    logger.error("Не удалось отвязать '{}' от {}: {}", email, orphan, e)
            else:
                logger.info(
                    "XUI-зомби: клиент '{}' сервера {}: нет ни одного InboundConnection — удаление с панели",
                    email, server.name,
                )
                try:
                    await xui_client.delete_client(email)
                    orphans_deleted += 1
                except Exception as e:
                    logger.error("Не удалось удалить зомби '{}' с панели: {}", email, e)

        marked_for_notify: list[dict] = []
        for conn, user_label in missing_from_synced:
            logger.info(
                "Зеркало: соединение {} (email={}) отсутствует на панели {} — помечаем error",
                conn.id, getattr(conn, "email", None), server.name,
            )
            conn.sync_status = "error"
            marked_for_notify.append({"email": conn.email, "user": user_label})

        await self.session.flush()

        logger.info(
            "Реконсиляция сервера {} (auto): удалено зомби={}, отвязано={}, помечено missing={}",
            server.name, orphans_deleted, orphans_detached, len(marked_for_notify),
        )

        if marked_for_notify:
            try:
                from app.services.notification_service import NotificationService

                await NotificationService(self.session).notify_admins_missing_on_panel(
                    server_name=server.name,
                    marked_connections=marked_for_notify,
                )
            except Exception as e:
                logger.error(
                    "Не удалось отправить уведомление о пропавших клиентах сервера {}: {}",
                    server.name, e,
                )

    async def _notify_divergences_ask(
        self, server: Server, batch_id: str, created: list, is_mass: bool
    ) -> None:
        """Отправить администраторам дайджест расхождений (режим ask)."""
        try:
            from app.services.notification_service import NotificationService

            await NotificationService(self.session).notify_admins_divergences(
                server_name=server.name,
                batch_id=batch_id,
                pendings=created,
                is_mass=is_mass,
            )
        except Exception as e:
            logger.error(
                "Не удалось отправить дайджест расхождений сервера {}: {}", server.name, e
            )

    async def _notify_divergences_report(self, server: Server, findings: list) -> None:
        """Отправить администраторам сводку расхождений без действий (режим report)."""
        try:
            from app.services.notification_service import NotificationService

            await NotificationService(self.session).notify_admins_divergences_report(
                server_name=server.name,
                findings=findings,
            )
        except Exception as e:
            logger.error(
                "Не удалось отправить отчёт о расхождениях сервера {}: {}", server.name, e
            )

    async def _sync_inbound_clients(self, inbound: Inbound, xui_client: object | None = None) -> int:
        """Dispatch client sync to the appropriate protocol handler.

        Args:
            inbound: Inbound model.
            xui_client: XUI client instance (kept for backward compat, not used directly).

        Returns:
            Number of synchronized clients.
        """
        handler = for_inbound(inbound)
        if handler is None:
            logger.debug("Нет обработчика для inbound типа '{}', пропуск {}", inbound.type, inbound.id)
            return 0
        return await handler.sync_clients(self.session, inbound, xui_service=self._xui_service)

    # === INTEGRITY CHECK ===

    async def verify_connections_integrity(self) -> bool:
        """Проверить целостность всех подключений.

        Returns:
            True если целостность в порядке
        """
        from sqlalchemy import select
        from sqlalchemy.orm import with_polymorphic

        conn_poly = with_polymorphic(InboundConnection, "*")
        inbound_poly = with_polymorphic(Inbound, "*")

        result = await self.session.execute(
            select(conn_poly).options(
                selectinload(conn_poly.inbound.of_type(inbound_poly))
                .selectinload(inbound_poly.server)
                .selectinload(Server.xui_panel),
                selectinload(conn_poly.subscription).selectinload(Subscription.client)
            )
        )
        connections = result.scalars().all()

        stats = {"total": len(connections), "synced": 0, "error": 0, "offline": 0}

        for connection in connections:
            conn_dict = getattr(connection, "__dict__", {})
            status = conn_dict.get("sync_status", "synced")
            stats[status] = stats.get(status, 0) + 1

            # Дополнительная проверка: клиент существует в XUI?
            if status == "synced":
                try:
                    inbound = connection.inbound
                    if inbound:
                        if inbound.server and getattr(inbound.server, "sync_status", None) != "synced":
                            stats["offline"] += 1
                            continue

                        handler = for_inbound(inbound)
                        if handler is None:
                            continue

                        valid = await handler.verify_connection(
                            self.session, connection, xui_service=self._xui_service
                        )
                        if not valid:
                            stats["error"] += 1

                except Exception as e:
                    logger.debug("Не удалось проверить {}: {}", connection.uuid, e)

        await self.session.flush()
        logger.info("Статистика целостности: {}", stats)
        return stats["error"] == 0

    # === MANUAL SYNC ===

    async def manual_sync(self, entity_type: str, entity_id: int | None = None) -> dict:
        """Ручная синхронизация по запросу админа.

        Args:
            entity_type: Тип сущности ("all", "server", "connection")
            entity_id: ID сущности (опционально)

        Returns:
            Результаты синхронизации
        """
        results = {"synced": 0, "errors": 0, "details": []}

        logger.debug("manual_sync: entity_type={}, entity_id={}", entity_type, entity_id)

        if entity_type == "all":
            # Полная синхронизация
            logger.debug("Запуск _sync_cycle с force=True")
            sync_result = await self._sync_cycle(force=True)
            results["synced"] = sync_result.get("servers", 0)
            logger.debug("_sync_cycle завершён, результат={}", sync_result)
            return results

        if self._sync_lock.locked():
            logger.warning("Пропуск ручной синхронизации — другая синхронизация уже выполняется")
            results["errors"] += 1
            results["details"].append("Синхронизация уже выполняется")
            return results

        async with self._sync_lock:
            try:
                if entity_type == "server":
                    if entity_id:
                        logger.debug("Ручная синхронизация сервера {}", entity_id)
                        server = await self.session.get(
                            Server,
                            entity_id,
                            options=[
                                selectinload(Server.xui_panel),
                                selectinload(Server.awg_service),
                                selectinload(Server.mtproxy_service),
                                selectinload(Server.inbounds),
                            ],
                        )
                        if server:
                            await self.sync_server(server, force=True)
                            results["synced"] = 1  # Один сервер синхронизирован
                        else:
                            results["errors"] += 1
                    else:
                        logger.debug("Ручная синхронизация всех серверов")
                        # sync_all_servers уже синхронизирует клиентов внутри sync_server
                        synced_servers = await self.sync_all_servers(force=True)
                        results["synced"] = synced_servers
                        logger.debug("Синхронизировано серверов: {}", synced_servers)

                elif entity_type == "connection" and entity_id:
                    logger.debug("Ручная синхронизация подключения {}", entity_id)
                    connection = await self.session.get(InboundConnection, entity_id)
                    if connection:
                        # TODO: Реализовать двустороннюю синхронизацию подключений
                        connection.sync_status = "synced"
                        connection.last_sync_at = datetime.now(UTC)
                        results["synced"] += 1
                    else:
                        results["errors"] += 1

            except Exception as e:
                logger.error("Ошибка ручной синхронизации: {} - {}", type(e).__name__, str(e), exc_info=True)
                results["errors"] += 1
                results["details"].append(f"{type(e).__name__}: {str(e)}")

        logger.debug("manual_sync завершён, результат={}", results)
        return results


# Импорт asyncio для использования в start_background_sync
