"""AWG protocol sync: expiry enable/disable + интероп с официальным клиентом."""

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import selectinload, with_polymorphic

from app.services.protocol_sync import ProtocolSyncBase, register

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.database.models import (
        AWGService,
        Inbound,
        InboundConnection,
        Server,
    )
    from app.services.vpn_providers.amnezia_awg import AmneziaAWGProvider
    from app.services.xui_service import XUIService


class AWGProtocolSync(ProtocolSyncBase):
    """Sync service for AmneziaWG.

    Три обязанности (docs/amnezia-awg-parallel-management.md):
    1. Wipe-детектор — сверка серверных ключей БД↔контейнер: расхождение значит,
       контейнер переустановили извне («Сохранить» в настройках AWG официального
       приложения делает docker rm -fv и генерирует новые ключи). До восстановления
       выдача конфигов блокируется через AWGService.wipe_detected.
    2. Expiry-цикл — enable/disable по сроку подписки; неудачные попытки
       помечаются pending_push и досылаются в следующих циклах.
    3. Внешние пиры — пиры живого awg0.conf, которых нет в БД (добавлены
       официальным клиентом): уведомление админу о новых и исчезнувших.
    """

    async def sync_clients(
        self,
        session: "AsyncSession",
        inbound: "Inbound",
        xui_service: "XUIService | None" = None,
    ) -> int:
        from app.database.models import AWGService, InboundConnection, Subscription
        from app.services.vpn_providers.factory import get_vpn_provider

        conn_poly = with_polymorphic(InboundConnection, "*")
        result = await session.execute(
            select(conn_poly)
            .where(conn_poly.inbound_id == inbound.id)
            .options(
                selectinload(conn_poly.subscription).selectinload(Subscription.client),
            )
        )
        connections = result.scalars().all()

        if not connections:
            return 0

        server = inbound.server
        try:
            provider = get_vpn_provider(server, inbound_type="awg_inbound")
        except ValueError:
            logger.warning("AWG sync: нет провайдера для сервера {}", server.id)
            return 0

        awg_service = (await session.execute(
            select(AWGService).where(AWGService.server_id == server.id)
        )).scalar_one_or_none()

        now = datetime.now(UTC)

        # Wipe-детектор: при расхождении ключей пиры не трогаем — иначе цикл
        # «восстановит» пиров в чужой переустановленный контейнер.
        if await self._check_wipe(provider, awg_service, server, session):
            await provider.close()
            await session.flush()
            return 0

        synced = await self._sync_expiry(provider, inbound, connections, now)
        await self._sync_external_peers(provider, awg_service, server, connections, session)

        await provider.close()
        await session.flush()
        return synced

    # ── 1. Wipe-детектор ──────────────────────────────────────────────

    async def _check_wipe(
        self,
        provider: "AmneziaAWGProvider",
        awg_service: "AWGService | None",
        server: "Server",
        session: "AsyncSession",
    ) -> bool:
        """Сверить серверные ключи из БД с живыми ключами контейнера.

        True = wipe (конейнер переустановили извне). Временный сбой SSH — не wipe.
        Сравниваются только пары «значение есть и в БД, и на сервере»: пустой
        pubkey в БД (легаси-строки) не должен давать ложную тревогу.
        """
        if awg_service is None:
            return False

        try:
            live = await provider.get_live_server_keys()
        except Exception as e:
            logger.warning(
                "AWG wipe-детектор {}: не удалось прочитать живые ключи ({})",
                server.id, e,
            )
            return False

        mismatches = []
        live_pub = live.get("server_public_key")
        if awg_service.server_public_key and live_pub and awg_service.server_public_key != live_pub:
            mismatches.append("серверный публичный ключ")

        stored_hpk = (awg_service.obfuscation or {}).get("HeaderProtectionKey")
        live_hpk = live.get("HeaderProtectionKey")
        if stored_hpk and live_hpk and stored_hpk != live_hpk:
            mismatches.append("HeaderProtectionKey")

        if not mismatches:
            if awg_service.wipe_detected:
                awg_service.wipe_detected = False
                logger.info("AWG {}: серверные ключи снова совпадают — wipe снят", server.id)
            return False

        if not awg_service.wipe_detected:
            awg_service.wipe_detected = True
            logger.error(
                "AWG wipe на сервере {}: расхождение ({}) — контейнер переустановлен извне",
                server.id, ", ".join(mismatches),
            )
            await self._notify_wipe(session, server, mismatches)
        return True

    async def _notify_wipe(
        self, session: "AsyncSession", server: "Server", mismatches: list[str]
    ) -> None:
        try:
            from app.services.notification_service import NotificationService

            service = NotificationService(session)
            await service.notify_admins_awg_wipe(server.name, mismatches)
        except Exception as e:
            logger.warning("Не удалось отправить уведомление о wipe админам: {}", e)

    # ── 2. Expiry-цикл + досылка pending_push ─────────────────────────

    async def _sync_expiry(
        self,
        provider: "AmneziaAWGProvider",
        inbound: "Inbound",
        connections: "list[InboundConnection]",
        now: datetime,
    ) -> int:
        from app.database.models import AWGInboundConnection

        synced = 0

        for conn in connections:
            try:
                if not isinstance(conn, AWGInboundConnection):
                    continue

                sub = conn.subscription
                client_name = sub.client.name if sub and sub.client else "—"

                # Срок берём из подписки (источник истины): conn.expiry_date может
                # отстать (напр. если update_client упал и локальная синхронизация
                # пропустилась) и вызвать ложное отключение активной подписки.
                expiry = sub.expiry_date if sub is not None else conn.expiry_date
                if expiry and expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=UTC)

                should_be_enabled = sub.is_active if sub else True
                if expiry and now > expiry:
                    should_be_enabled = False

                if conn.sync_status == "pending_push":
                    # Досылка: флаг ставится, когда серверная операция из
                    # new_subscription_service/предыдущего цикла не доехала.
                    # Проверяем фактическое состояние пира и доводим до нужного.
                    if not conn.public_key:
                        conn.sync_status = "error"
                        continue
                    in_config = await provider._peer_in_config(conn.public_key)
                    if should_be_enabled and not in_config:
                        if await provider.enable_client(inbound, conn):
                            conn.is_enabled = True
                            conn.sync_status = "synced"
                            conn.last_sync_at = now
                            synced += 1
                    elif not should_be_enabled and in_config:
                        if await provider.disable_client(inbound, conn):
                            conn.is_enabled = False
                            conn.sync_status = "synced"
                            conn.last_sync_at = now
                            synced += 1
                    else:
                        # Сервер уже в нужном состоянии — снимаем флаг.
                        conn.sync_status = "synced"
                        conn.is_enabled = should_be_enabled
                        conn.last_sync_at = now
                    continue

                if conn.is_enabled and not should_be_enabled:
                    if await provider.disable_client(inbound, conn):
                        conn.is_enabled = False
                        conn.sync_status = "synced"
                        conn.last_sync_at = now
                        logger.info(
                            "AWG: подключение {} ({}) отключено (истёк срок)",
                            conn.id, client_name,
                        )
                        synced += 1
                    else:
                        conn.sync_status = "pending_push"
                        logger.warning(
                            "AWG: не удалось отключить подключение {} ({}) на сервере — "
                            "повтор в следующем цикле",
                            conn.id, client_name,
                        )

                elif not conn.is_enabled and should_be_enabled:
                    if await provider.enable_client(inbound, conn):
                        conn.is_enabled = True
                        conn.sync_status = "synced"
                        conn.last_sync_at = now
                        logger.info(
                            "AWG: подключение {} ({}) включено (подписка возобновлена)",
                            conn.id, client_name,
                        )
                        synced += 1
                    else:
                        conn.sync_status = "pending_push"
                        logger.warning(
                            "AWG: не удалось включить подключение {} ({}) на сервере "
                            "(повтор в следующем цикле)",
                            conn.id, client_name,
                        )

            except Exception as e:
                logger.warning("AWG sync: ошибка обработки подключения {}: {}", conn.id, e)

        return synced

    # ── 3. Внешние пиры (официальный клиент) ──────────────────────────

    async def _sync_external_peers(
        self,
        provider: "AmneziaAWGProvider",
        awg_service: "AWGService | None",
        server: "Server",
        connections: "list[InboundConnection]",
        session: "AsyncSession",
    ) -> None:
        """Обнаружить пиров, добавленных вне бота, и уведомить админа об изменениях.

        Повторные уведомления подавляются через AWGService.external_peers_seen
        (список pubkey'ов прошлого цикла). Пиры не удаляются и не модифицируются.
        """
        from app.database.models import AWGInboundConnection

        if awg_service is None:
            return

        own_keys = {
            c.public_key
            for c in connections
            if isinstance(c, AWGInboundConnection) and c.public_key
        }

        try:
            peers = await provider.list_peers()
        except Exception as e:
            logger.warning(
                "AWG {}: не удалось прочитать живых пиров для external-детекта ({})",
                server.id, e,
            )
            return

        external = [p for p in peers if p["public_key"] not in own_keys]
        live_keys = sorted(p["public_key"] for p in external)
        seen = set(awg_service.external_peers_seen or [])

        new_peers = [p for p in external if p["public_key"] not in seen]
        gone_keys = seen - set(live_keys)

        if awg_service.external_peers_seen != live_keys:
            awg_service.external_peers_seen = live_keys

        if not new_peers and not gone_keys:
            return

        logger.info(
            "AWG {}: внешние пиры изменились (новых: {}, исчезло: {})",
            server.id, len(new_peers), len(gone_keys),
        )
        await self._notify_external_peers(session, server, new_peers, gone_keys)

    async def _notify_external_peers(
        self,
        session: "AsyncSession",
        server: "Server",
        new_peers: list[dict],
        gone_keys: set[str],
    ) -> None:
        try:
            from app.services.notification_service import NotificationService

            service = NotificationService(session)
            await service.notify_admins_awg_external_peers(
                server.name, new_peers, sorted(gone_keys)
            )
        except Exception as e:
            logger.warning("Не удалось отправить уведомление о внешних пирax админам: {}", e)

    # ── Целостность ───────────────────────────────────────────────────

    async def verify_connection(
        self,
        session: "AsyncSession",
        connection: "InboundConnection",
        xui_service: "XUIService | None" = None,
    ) -> bool:
        from app.database.models import AWGInboundConnection

        try:
            if not isinstance(connection, AWGInboundConnection):
                return True
            if not connection.public_key:
                return False

            inbound = connection.inbound
            server = inbound.server if inbound is not None else None
            if server is None:
                return True

            awg_service = getattr(server, "awg_service", None)
            if awg_service is not None and awg_service.wipe_detected:
                return False

            from app.services.vpn_providers.factory import get_vpn_provider

            provider = get_vpn_provider(server, inbound_type="awg_inbound")
            try:
                return await provider._peer_in_config(connection.public_key)
            finally:
                await provider.close()
        except Exception as e:
            # Сбой проверки (SSH и т.п.) — не повод считать соединение битым.
            logger.debug("AWG verify_connection {}: недоступен ({})", connection.id, e)
            return True


async def recover_after_wipe(session: "AsyncSession", server: "Server") -> str:
    """Восстановление после wipe без переустановки контейнера.

    Перечитывает новые серверные ключи/PSK/обфускацию из переустановленного
    контейнера, обновляет AWGService и возвращает включённых пиров из БД
    (их прежними клиентскими ключами, но с новым PSK). Старые клиентские
    конфиги мертвы — серверные ключи сменились, подписчиков нужно перевыпустить.

    Returns:
        Человекочитаемый итог для админа.
    """
    from app.database.models import AWGInboundConnection, Inbound
    from app.services.installers.awg_installer import AWGInstaller
    from app.services.ssh_service import SSHManager
    from app.services.vpn_providers.factory import get_vpn_provider

    awg = server.awg_service
    if awg is None:
        raise ValueError("На сервере не настроен AWG")

    installer = AWGInstaller(SSHManager(server, session=session))
    discovered = await installer.discover_existing(awg.container_name)

    awg.port = discovered["port"]
    awg.subnet_ip = discovered["subnet_ip"]
    awg.subnet_cidr = discovered["subnet_cidr"]
    awg.obfuscation = discovered["obfuscation"]
    if discovered["server_public_key"]:
        awg.server_public_key = discovered["server_public_key"]
    if discovered["server_private_key"]:
        awg.server_private_key = discovered["server_private_key"]
    awg.wipe_detected = False
    awg.external_peers_seen = []

    provider = get_vpn_provider(server, inbound_type="awg_inbound")
    try:
        new_psk = (await provider._get_server_psk()).strip()

        inbound = (await session.execute(
            select(Inbound).where(
                Inbound.server_id == server.id, Inbound.protocol == "awg"
            )
        )).scalar_one_or_none()

        restored = 0
        total_enabled = 0
        if inbound is not None:
            conns = (await session.execute(
                select(AWGInboundConnection).where(
                    AWGInboundConnection.inbound_id == inbound.id
                )
            )).scalars().all()
            for conn in conns:
                # PSK контейнера новый (старый умер вместе с контейнером) — без
                # обновления re-add добавит пира с недействительным PSK.
                conn.psk = new_psk
                if conn.is_enabled:
                    total_enabled += 1
                    if await provider.enable_client(inbound, conn):
                        restored += 1
    finally:
        await provider.close()

    await session.flush()
    return (
        f"Серверные ключи обновлены, пиров возвращено: {restored}/{total_enabled}. "
        "⚠️ Прежние клиентские конфиги недействительны (серверные ключи сменились) — "
        "перевыпустите vpn:// подписчикам."
    )


register("awg_inbound", AWGProtocolSync())
