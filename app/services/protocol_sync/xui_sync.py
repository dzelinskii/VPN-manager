"""XUI protocol sync service."""

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from loguru import logger
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select
from sqlalchemy.orm import selectinload, with_polymorphic

from app.services.protocol_sync import ProtocolSyncBase, register
from app.xui_client.models import ensure_settings_dict

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.database.models import Inbound, InboundConnection
    from app.services.xui_service import XUIService


class XUIProtocolSync(ProtocolSyncBase):
    """Synchronizes XUI inbound clients with the 3x-ui panel."""

    async def sync_clients(
        self,
        session: "AsyncSession",
        inbound: "Inbound",
        xui_service: "XUIService | None" = None,
    ) -> int:
        if xui_service is None:
            logger.warning("XUI sync: нет xui_service для inbound {}", inbound.id)
            return 0

        xui_client = await xui_service._get_client(inbound.server)
        if xui_client is None:
            logger.warning("XUI sync: не удалось получить XUI-клиент для inbound {}", inbound.id)
            return 0

        logger.info(
            "XUI sync_clients: начало для inbound {} (xui_id: {}, remark: {})",
            inbound.id, inbound.xui_id, inbound.remark,
        )

        xui_inbound = await xui_client.get_inbound(inbound.xui_id)

        if not xui_inbound:
            logger.warning("Inbound {} не найден на панели", inbound.xui_id)
            return 0

        if not xui_inbound.settings:
            logger.warning("Inbound {} не имеет настроек клиентов", inbound.id)
            return 0

        try:
            settings_dict = ensure_settings_dict(xui_inbound.settings)
            xui_clients = settings_dict.get("clients", [])
        except (json.JSONDecodeError, TypeError) as e:
            logger.error("Ошибка парсинга settings для inbound {}: {}", inbound.id, e)
            return 0

        logger.debug(
            "XUI sync_clients: получено {} клиентов из inbound {} ({})",
            len(xui_clients), inbound.id, inbound.remark,
        )
        if xui_clients:
            logger.debug("Пример данных клиента: {}", xui_clients[0])

        from app.database.models import Inbound, InboundConnection, Subscription

        conn_poly = with_polymorphic(InboundConnection, "*")
        state = sa_inspect(inbound)
        if "client_connections" in state.unloaded:
            existing_connections_result = await session.execute(
                select(conn_poly)
                .where(conn_poly.inbound_id == inbound.id)
                .options(
                    selectinload(conn_poly.subscription).selectinload(Subscription.client),
                    selectinload(conn_poly.inbound).selectinload(Inbound.server),
                )
            )
            existing_connections = list(existing_connections_result.scalars())
        else:
            existing_connections = list(inbound.client_connections)

        existing_map = {
            getattr(conn, "uuid", None): conn
            for conn in existing_connections
            if getattr(conn, "uuid", None)
        }
        logger.debug(
            "XUI sync_clients: в базе найдено {} подключений для inbound {}",
            len(existing_map), inbound.id,
        )

        synced_count = 0
        for xui_client_data in xui_clients:
            xui_uuid = xui_client_data.get("id", "")
            if not xui_uuid:
                logger.warning("Клиент без UUID: {}", xui_client_data)
                continue

            if xui_uuid in existing_map:
                conn = existing_map[xui_uuid]

                # sync_status="error" означает, что наше изменение до панели не
                # доехало. Принять её данные — молча потерять продление или
                # смену статуса, поэтому оставляем локальное состояние как есть.
                if conn.sync_status == "error":
                    logger.warning(
                        "Подключение {} в статусе error — не принимаем данные панели, "
                        "чтобы не потерять непрошедшее изменение",
                        conn.id,
                    )
                    continue

                xui_enable = xui_client_data.get("enable", True)
                xui_total_gb = xui_client_data.get("totalGB", 0) // (1024 * 1024 * 1024)
                xui_expiry_time = xui_client_data.get("expiryTime", 0)

                logger.debug(
                    "Синхронизация клиента {}: enable={}, totalGB={}, expiry={}",
                    conn.uuid, xui_enable, xui_total_gb, xui_expiry_time,
                )

                if conn.is_enabled != xui_enable:
                    old_status = conn.is_enabled
                    conn.is_enabled = xui_enable
                    logger.info(
                        "Подключение {} ({}): is_enabled={} → {}",
                        conn.id, conn.uuid, old_status, xui_enable,
                    )

                if conn.total_gb != xui_total_gb:
                    old_gb = conn.total_gb
                    conn.total_gb = xui_total_gb
                    logger.info(
                        "Подключение {} ({}): total_gb {}GB → {}GB",
                        conn.id, conn.uuid, old_gb, xui_total_gb,
                    )

                new_expiry = None
                if xui_expiry_time > 0:
                    new_expiry = datetime.fromtimestamp(xui_expiry_time / 1000, tz=UTC)

                if conn.expiry_date != new_expiry:
                    old_expiry = conn.expiry_date
                    conn.expiry_date = new_expiry
                    logger.info(
                        "Подключение {} ({}): expiry {} → {}",
                        conn.id, conn.uuid, old_expiry, new_expiry,
                    )

                if conn.subscription:
                    subscription = conn.subscription

                    if subscription.total_gb != xui_total_gb:
                        old_gb = subscription.total_gb
                        subscription.total_gb = xui_total_gb
                        logger.info(
                            "Подписка {}: total_gb {}GB → {}GB",
                            subscription.id, old_gb, xui_total_gb,
                        )

                    if subscription.expiry_date != new_expiry:
                        old_expiry = subscription.expiry_date
                        subscription.expiry_date = new_expiry
                        logger.info(
                            "Подписка {}: expiry {} → {}",
                            subscription.id, old_expiry, new_expiry,
                        )

                conn.sync_status = "synced"
                conn.last_sync_at = datetime.now(UTC)
                synced_count += 1
            else:
                logger.info(
                    "Клиент {} найден на панели, но отсутствует в базе (создан вручную)",
                    xui_uuid,
                )

        await session.flush()
        logger.info("Синхронизировано {} клиентов для inbound {}", synced_count, inbound.id)
        return synced_count

    async def verify_connection(
        self,
        session: "AsyncSession",
        connection: "InboundConnection",
        xui_service: "XUIService | None" = None,
    ) -> bool:
        if xui_service is None:
            return True

        inbound = connection.inbound
        if not inbound or "server" not in getattr(inbound, "__dict__", {}):
            return True

        if not inbound.server.xui_panel:
            return True

        try:
            xui_client = await xui_service._get_client(inbound.server)
            c_email = getattr(connection, "email", None)
            if not c_email and isinstance(getattr(connection, "provider_payload", None), dict):
                c_email = connection.provider_payload.get("email")
            if not c_email:
                # Cannot verify without email; skip silently
                return True
            xui_data = await xui_client.get_client(c_email)

            if not xui_data:
                connection.sync_status = "error"
                connection.sync_error = "Client missing in XUI (deleted manually?)"
                logger.warning("Клиент {} не найден в XUI", connection.uuid)
                return False
        except Exception as e:
            logger.debug("Не удалось проверить {}: {}", connection.uuid, e)

        return True


register("xui_inbound", XUIProtocolSync())
