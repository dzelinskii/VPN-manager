"""Subscription service for managing client subscriptions."""

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.models import (
    AWGInboundConnection,
    Client,
    Inbound,
    InboundConnection,
    MTProxyInboundConnection,
    Server,
    Subscription,
    XUIInboundConnection,
)
from app.services.vpn_providers import BaseVPNProvider, get_vpn_provider
from app.utils import generate_subscription_token
from app.utils.date_utils import ensure_utc
from app.xui_client.exceptions import XUIError


class NewSubscriptionService:
    """Service for subscription management with new architecture."""

    def __init__(self, session: AsyncSession) -> None:
        """Initialize service with database session.

        Args:
            session: Async database session
        """
        self.session = session
        self._providers: dict[int, BaseVPNProvider] = {}

    async def __aenter__(self):
        """Enter async context."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Exit async context and close all clients."""
        await self.close_all_clients()

    # Client methods

    async def get_client_subscriptions(self, client_id: int) -> Sequence[Subscription]:
        """Get all subscriptions for client.

        Args:
            client_id: Client ID

        Returns:
            List of subscriptions
        """
        result = await self.session.execute(
            select(Subscription)
            .where(Subscription.client_id == client_id)
            .options(
                selectinload(Subscription.client),
                selectinload(Subscription.inbound_connections)
                .selectinload(InboundConnection.inbound)
                .selectinload(Inbound.server)
                .selectinload(Server.xui_panel),
                selectinload(Subscription.inbound_connections)
                .selectinload(InboundConnection.inbound)
                .selectinload(Inbound.server)
                .selectinload(Server.awg_service),
                selectinload(Subscription.inbound_connections)
                .selectinload(InboundConnection.inbound)
                .selectinload(Inbound.server)
                .selectinload(Server.mtproxy_service)
            )
            .order_by(Subscription.created_at.desc())
        )
        return result.scalars().all()

    # Subscription methods

    async def get_subscription(self, subscription_id: int) -> Subscription | None:
        """Get subscription by ID.

        Args:
            subscription_id: Subscription ID

        Returns:
            Subscription or None
        """
        result = await self.session.execute(
            select(Subscription)
            .where(Subscription.id == subscription_id)
            .options(
                selectinload(Subscription.client),
                selectinload(Subscription.template),
                selectinload(Subscription.inbound_connections)
                .selectinload(InboundConnection.inbound)
                .selectinload(Inbound.server)
                .selectinload(Server.xui_panel),
                selectinload(Subscription.inbound_connections)
                .selectinload(InboundConnection.inbound)
                .selectinload(Inbound.server)
                .selectinload(Server.awg_service),
                selectinload(Subscription.inbound_connections)
                .selectinload(InboundConnection.inbound)
                .selectinload(Inbound.server)
                .selectinload(Server.mtproxy_service)
            )
        )
        return result.scalar_one_or_none()

    async def create_subscription(
        self,
        client_id: int,
        name: str,
        total_gb: int = 0,
        expiry_days: int | None = None,
        notes: str | None = None,
        template_id: int | None = None,
    ) -> tuple[Subscription, list[InboundConnection]]:
        """Create a new subscription.

        Args:
            client_id: Client ID
            name: Subscription name
            total_gb: Traffic limit in GB (0 = unlimited)
            expiry_days: Days until expiry (None = never)
            notes: Optional notes
            template_id: Optional template ID used to create this subscription

        Returns:
            A tuple containing the created subscription and an empty list of connections.
        """
        # Calculate expiry date
        expiry_date = None
        if expiry_days:
            expiry_date = datetime.now(UTC) + timedelta(days=expiry_days)

        # Generate unique token
        subscription = Subscription(
            client_id=client_id,
            template_id=template_id,
            name=name,
            subscription_token=generate_subscription_token(),
            total_gb=total_gb,
            expiry_date=expiry_date,
            notes=notes,
            is_active=True,
        )
        self.session.add(subscription)
        await self.session.flush()

        # Reload with relationships
        reloaded_subscription = await self.get_subscription(subscription.id)
        if not reloaded_subscription:
            raise XUIError("Subscription not found after creation")

        # Return subscription and an empty list for connections, as they are created later
        return reloaded_subscription, []

    async def rebuild_subscription(
        self,
        subscription_id: int,
        new_name: str,
        new_total_gb: int,
        new_expiry_days: int | None,
        new_inbound_ids: list[int],
        template_id: int | None = None,
        notes: str | None = None,
    ) -> tuple[Subscription, list[InboundConnection]]:
        """Rebuild subscription with new configuration while keeping token/UUID.

        Args:
            subscription_id: ID of the existing subscription
            new_name: New subscription name
            new_total_gb: New traffic limit in GB (0 = unlimited)
            new_expiry_days: New expiry days
            new_inbound_ids: New list of inbound IDs
            template_id: Optional template ID if rebuilt from template
            notes: Optional notes

        Returns:
            A tuple containing the updated subscription and list of connections.
        """
        subscription = await self.get_subscription(subscription_id)
        if not subscription:
            raise XUIError("Subscription not found")

        current_connections = subscription.inbound_connections
        current_inbound_ids = {c.inbound_id for c in current_connections}
        new_inbound_ids_set = set(new_inbound_ids)

        # Update subscription properties
        subscription.name = new_name
        subscription.total_gb = new_total_gb
        subscription.template_id = template_id
        if notes is not None:
            subscription.notes = notes

        expiry_date = None
        if new_expiry_days:
            expiry_date = datetime.now(UTC) + timedelta(days=new_expiry_days)
        subscription.expiry_date = expiry_date

        await self.session.flush()

        # Determine removed, added, and kept inbounds
        removed_ids = current_inbound_ids - new_inbound_ids_set
        added_ids = new_inbound_ids_set - current_inbound_ids
        kept_ids = current_inbound_ids & new_inbound_ids_set

        failed: list[tuple[int, str]] = []

        # Process removed
        for ib_id in removed_ids:
            try:
                removed_ok = await self.remove_inbound_from_subscription(subscription_id, ib_id)
                if not removed_ok:
                    logger.error(
                        "rebuild_subscription: remove_inbound_from_subscription вернул False "
                        "для inbound {} sub {} (phantom помечен error)",
                        ib_id, subscription_id,
                    )
                    failed.append((ib_id, "db delete failed"))
            except Exception as e:
                logger.error(
                    "rebuild_subscription: не удалось удалить inbound {} для sub {}: {}",
                    ib_id, subscription_id, e,
                )
                failed.append((ib_id, str(e)))

        # Process kept (reset traffic, update limits and expiry)
        for conn in current_connections:
            if conn.inbound_id in kept_ids:
                conn.total_gb = new_total_gb
                conn.expiry_date = expiry_date

                try:
                    inbound = conn.inbound
                    provider = await self._get_provider(inbound.server, inbound=inbound)

                    await provider.reset_client_traffic(inbound, conn)

                    conn.is_enabled = True
                    await provider.update_client(inbound, conn, new_total_gb, expiry_date)
                except Exception as e:
                    logger.error(
                        "rebuild_subscription: не удалось обновить inbound {} для sub {}: {}",
                        conn.inbound_id, subscription_id, e,
                    )
                    conn.sync_status = "error"
                    failed.append((conn.inbound_id, str(e)))

        # Process added — XUI одной панели группируются в одного клиента (attach к
        # существующему), AWG/MTProxy создаются по одному.
        if added_ids:
            try:
                await self.add_inbounds_to_subscription(subscription_id, list(added_ids))
            except Exception as e:
                logger.error(
                    "rebuild_subscription: не удалось добавить inbounds {} для sub {}: {}",
                    sorted(added_ids), subscription_id, e,
                )
                failed.append((next(iter(added_ids)), str(e)))

        await self.session.flush()

        # Reload to get fresh connections
        reloaded_subscription = await self.get_subscription(subscription.id)

        if failed:
            raise XUIError(
                f"Rebuild partially failed for subscription {subscription_id}. "
                f"Failed inbounds (id, error): {failed}"
            )

        return reloaded_subscription, list(reloaded_subscription.inbound_connections)

    # Inbound Connection methods

    async def add_inbound_to_subscription(
        self,
        subscription_id: int,
        inbound_id: int,
        client_uuid: str | None = None,
        mtproxy_domain: str | None = None,
    ) -> InboundConnection:
        """Add inbound connection to subscription.

        Args:
            subscription_id: Subscription ID
            inbound_id: Inbound ID
            client_uuid: Optional UUID to use (for rebuilding subscriptions)
            mtproxy_domain: Optional domain for MTProxy mtg-multi secret generation
        """
        # Check if inbound already exists in subscription
        existing = await self.session.execute(
            select(InboundConnection).where(
                InboundConnection.subscription_id == subscription_id,
                InboundConnection.inbound_id == inbound_id,
            )
        )
        if existing.scalar_one_or_none():
            raise XUIError("Inbound already exists in this subscription")

        # Get subscription and inbound
        subscription = await self.get_subscription(subscription_id)
        if not subscription:
            raise XUIError("Subscription not found")

        inbound_result = await self.session.execute(
            select(Inbound).where(Inbound.id == inbound_id).options(
                selectinload(Inbound.server).selectinload(Server.xui_panel),
                selectinload(Inbound.server).selectinload(Server.awg_service),
                selectinload(Inbound.server).selectinload(Server.mtproxy_service),
            )
        )
        inbound = inbound_result.scalar_one_or_none()
        if not inbound:
            raise XUIError("Inbound not found")

        # Generate UUID if not provided
        import uuid

        client_uuid = client_uuid or str(uuid.uuid4())
        client_email = None

        try:
            provider = await self._get_provider(inbound.server, inbound=inbound)
        except Exception as e:
            raise XUIError(f"Failed to get VPN provider: {e}") from e

        try:
            provider_kwargs = {
                "inbound": inbound,
                "subscription": subscription,
                "client_uuid": client_uuid,
                "email": None,
            }
            if mtproxy_domain and inbound.type == "mtproxy_inbound":
                provider_kwargs["domain"] = mtproxy_domain
            client_data = await provider.add_client(**provider_kwargs)
            client_uuid = client_data.get("uuid", client_uuid)
            client_email = client_data.get("email")
            provider_payload = client_data
        except Exception as e:
            logger.error("Не удалось создать клиента на VPN-панели: {}", e, exc_info=True)
            raise XUIError(f"Failed to create client in VPN panel: {str(e)}") from e

        # Лёгкий объект для saga-компенсации: provider.remove_client читает у
        # connection только поля идентификации. ORM-инстанс через __new__ нельзя —
        # без _sa_instance_state присваивание мапленых атрибутов падает в рантайме.
        def _build_temp_connection() -> SimpleNamespace:
            """Build an unsaved namespace from client_data for saga compensation."""
            if inbound.type == "xui_inbound":
                return SimpleNamespace(
                    email=provider_payload.get("email", client_email),
                    uuid=provider_payload.get("uuid", client_uuid),
                    xui_client_id=provider_payload.get("xui_client_id", client_uuid),
                    provider_payload=provider_payload,
                    public_key=None,
                    secret=None,
                )
            if inbound.type == "awg_inbound":
                return SimpleNamespace(
                    public_key=provider_payload.get("public_key"),
                    email=None,
                    secret=None,
                    provider_payload=None,
                )
            if inbound.type == "mtproxy_inbound":
                return SimpleNamespace(
                    secret=provider_payload.get("secret"),
                    email=None,
                    public_key=None,
                    provider_payload=None,
                )
            return SimpleNamespace(email=None, public_key=None, secret=None, provider_payload=None)

        async with self.session.begin_nested():
            try:
                base_kwargs = {
                    "subscription_id": subscription_id,
                    "inbound_id": inbound_id,
                    "is_enabled": True,
                    "total_gb": subscription.total_gb,
                    "expiry_date": subscription.expiry_date,
                    "sync_status": "synced",
                    "last_sync_at": datetime.now(UTC),
                }

                if inbound.type == "xui_inbound":
                    xui_kwargs = {
                        "provider_payload": provider_payload,
                        "uuid": provider_payload.get("uuid", client_uuid),
                        "email": provider_payload.get("email", client_email),
                        "xui_client_id": provider_payload.get("xui_client_id", client_uuid),
                    }
                    connection = XUIInboundConnection(**base_kwargs, **xui_kwargs)
                elif inbound.type == "awg_inbound":
                    connection = AWGInboundConnection(
                        **base_kwargs,
                        client_ip=provider_payload.get("client_ip"),
                        public_key=provider_payload.get("public_key"),
                        private_key=provider_payload.get("private_key"),
                        psk=provider_payload.get("psk"),
                    )
                elif inbound.type == "mtproxy_inbound":
                    connection = MTProxyInboundConnection(
                        **base_kwargs,
                        secret=provider_payload.get("secret"),
                        domain=provider_payload.get("domain"),
                    )
                else:
                    connection = InboundConnection(**base_kwargs)

                connection.inbound = inbound
                connection.subscription = subscription

                self.session.add(connection)
                await self.session.flush()

                if hasattr(inbound, "client_count"):
                    inbound.client_count += 1

                return connection

            except Exception as db_error:
                logger.error("Не удалось сохранить inbound-соединение: {}", db_error, exc_info=True)
                # Saga compensation: remove the client we just created on the panel.
                temp_conn = _build_temp_connection()
                try:
                    await provider.remove_client(inbound, temp_conn)
                    logger.info(
                        "Saga compensation succeeded: removed panel client after DB failure "
                        "(inbound_id=%s, subscription_id=%s)",
                        inbound_id,
                        subscription_id,
                    )
                except Exception as comp_error:
                    logger.critical(
                        "Zombie client left on panel after DB failure and failed compensation: "
                        "inbound_id=%s subscription_id=%s error=%s",
                        inbound_id,
                        subscription_id,
                        comp_error,
                    )
                raise XUIError(f"Failed to save inbound connection: {str(db_error)}") from db_error

    async def add_xui_inbounds_to_subscription(
        self, subscription_id: int, inbound_ids: list[int]
    ) -> list[InboundConnection]:
        """Создать ОДИН панельный клиент на все переданные XUI-inbound'ы одной
        панели и сохранить по строке на каждый inbound (общие uuid/email).

        Все inbound_ids должны принадлежать одной XUI-панели (одному серверу) —
        вызывающий код группирует их по серверу. Так панель v3.2.5+ не отвергает
        повторный subId: один клиент (один subId) привязан сразу ко всем
        inbound'ам через inboundIds.
        """
        if not inbound_ids:
            return []

        subscription = await self.get_subscription(subscription_id)
        if not subscription:
            raise XUIError("Subscription not found")

        existing_result = await self.session.execute(
            select(InboundConnection.inbound_id).where(
                InboundConnection.subscription_id == subscription_id,
                InboundConnection.inbound_id.in_(inbound_ids),
            )
        )
        already = set(existing_result.scalars().all())
        target_ids = [i for i in inbound_ids if i not in already]
        if not target_ids:
            return []

        inbound_result = await self.session.execute(
            select(Inbound).where(Inbound.id.in_(target_ids)).options(
                selectinload(Inbound.server).selectinload(Server.xui_panel),
                selectinload(Inbound.server).selectinload(Server.awg_service),
                selectinload(Inbound.server).selectinload(Server.mtproxy_service),
            )
        )
        inbounds = list(inbound_result.scalars().all())
        if not inbounds:
            raise XUIError("Inbounds not found")

        server = inbounds[0].server
        if any(ib.server_id != server.id for ib in inbounds):
            raise XUIError("add_xui_inbounds_to_subscription: inbound'ы из разных панелей")
        try:
            provider = await self._get_provider(server, inbound=inbounds[0])
        except Exception as e:
            raise XUIError(f"Failed to get VPN provider: {e}") from e

        # Уже ли у подписки есть клиент на этой панели? Тогда не плодим второго
        # (subId занят), а привязываем новые inbound'ы к существующему клиенту.
        existing_conn = (
            await self.session.execute(
                select(XUIInboundConnection)
                .join(Inbound, Inbound.id == XUIInboundConnection.inbound_id)
                .where(
                    XUIInboundConnection.subscription_id == subscription_id,
                    Inbound.server_id == server.id,
                    XUIInboundConnection.inbound_id.notin_(target_ids),
                )
                .limit(1)
            )
        ).scalar_one_or_none()

        xui_ids = [getattr(ib, "xui_id", ib.id) for ib in inbounds]
        attach_to_existing = existing_conn is not None and bool(existing_conn.email)

        if attach_to_existing:
            try:
                await provider.attach_inbounds(existing_conn.email, xui_ids)
            except Exception as e:
                logger.error("Не удалось привязать клиента к inbound'ам: {}", e, exc_info=True)
                raise XUIError(f"Failed to attach client to inbounds: {str(e)}") from e
            shared_uuid = existing_conn.uuid
            shared_email = existing_conn.email
            client_data = existing_conn.provider_payload or {
                "uuid": existing_conn.uuid,
                "email": existing_conn.email,
                "xui_client_id": existing_conn.xui_client_id,
            }
        else:
            try:
                client_data = await provider.add_client_to_inbounds(inbounds, subscription)
            except Exception as e:
                logger.error("Не удалось создать клиента на VPN-панели: {}", e, exc_info=True)
                raise XUIError(f"Failed to create client in VPN panel: {str(e)}") from e
            shared_uuid = client_data.get("uuid")
            shared_email = client_data.get("email")

        connections: list[InboundConnection] = []
        async with self.session.begin_nested():
            try:
                for inbound in inbounds:
                    connection = XUIInboundConnection(
                        subscription_id=subscription_id,
                        inbound_id=inbound.id,
                        is_enabled=True,
                        total_gb=subscription.total_gb,
                        expiry_date=subscription.expiry_date,
                        sync_status="synced",
                        last_sync_at=datetime.now(UTC),
                        provider_payload=client_data,
                        uuid=shared_uuid,
                        email=shared_email,
                        xui_client_id=client_data.get("xui_client_id", shared_uuid),
                    )
                    connection.inbound = inbound
                    connection.subscription = subscription
                    self.session.add(connection)
                    if hasattr(inbound, "client_count") and inbound.client_count is not None:
                        inbound.client_count += 1
                    connections.append(connection)
                await self.session.flush()
                return connections
            except Exception as db_error:
                logger.error(
                    "Не удалось сохранить XUI-соединения для подписки {} (inbounds={}): {}",
                    subscription_id, target_ids, db_error, exc_info=True,
                )
                # Компенсация: при attach — отвязать только новые inbound'ы (клиент с
                # прежними остаётся); при create — снять клиента целиком.
                try:
                    if attach_to_existing:
                        await provider.detach_inbounds(shared_email, xui_ids)
                    else:
                        tmp = SimpleNamespace(
                            email=shared_email, uuid=shared_uuid, provider_payload=client_data
                        )
                        await provider.remove_client(inbounds[0], tmp)
                except Exception:
                    logger.critical(
                        "Компенсация для подписки {} не удалась (email={}): возможен "
                        "рассинхрон, очистит реконсилятор.",
                        subscription_id, shared_email, exc_info=True,
                    )
                raise XUIError(
                    f"Failed to save inbound connections: {str(db_error)}"
                ) from db_error

    async def add_inbounds_to_subscription(
        self, subscription_id: int, inbound_ids: Iterable[int], mtproxy_domain: str | None = None
    ) -> list[InboundConnection]:
        """Добавить набор inbound'ов к подписке, группируя XUI одной панели в одного
        клиента (attach к существующему / один add), а AWG/MTProxy создавая по одному.

        Единая точка для всех путей «добавить inbound к подписке».
        """
        ids = list(inbound_ids)
        if not ids:
            return []

        rows = (
            await self.session.execute(
                select(Inbound.id, Inbound.type, Inbound.server_id).where(Inbound.id.in_(ids))
            )
        ).all()
        meta = {r.id: (r.type, r.server_id) for r in rows}

        xui_by_server: dict[int, list[int]] = {}
        others: list[int] = []
        for i in ids:
            t_s = meta.get(i)
            if t_s and t_s[0] == "xui_inbound":
                xui_by_server.setdefault(t_s[1], []).append(i)
            else:
                others.append(i)

        created: list[InboundConnection] = []
        for server_ids in xui_by_server.values():
            created.extend(await self.add_xui_inbounds_to_subscription(subscription_id, server_ids))
        for i in others:
            created.append(
                await self.add_inbound_to_subscription(
                    subscription_id, i, mtproxy_domain=mtproxy_domain
                )
            )
        return created

    async def remove_inbound_from_subscription(
        self,
        subscription_id: int,
        inbound_id: int,
    ) -> bool:
        """Remove inbound connection from subscription.

        Args:
            subscription_id: Subscription ID
            inbound_id: Inbound ID

        Returns:
            True if removed
        """
        conn_result = await self.session.execute(
            select(InboundConnection).where(
                InboundConnection.subscription_id == subscription_id,
                InboundConnection.inbound_id == inbound_id,
            )
        )
        connection = conn_result.scalar_one_or_none()
        if not connection:
            return False

        # Get inbound info with server relationship
        inbound_result = await self.session.execute(
            select(Inbound).where(Inbound.id == inbound_id).options(
                selectinload(Inbound.server).selectinload(Server.xui_panel),
                selectinload(Inbound.server).selectinload(Server.awg_service),
                selectinload(Inbound.server).selectinload(Server.mtproxy_service),
            )
        )
        inbound = inbound_result.scalar_one_or_none()

        # Delete from provider first. If the panel call fails, do not touch the DB
        # so state remains consistent (panel has the client, DB has the record).
        if inbound and inbound.server:
            provider = await self._get_provider(inbound.server, inbound=inbound)
            email = getattr(connection, "email", None)
            if getattr(inbound, "type", None) == "xui_inbound" and email:
                # Есть ли у подписки другие inbound'ы на том же клиенте (общий email)?
                others = (
                    await self.session.execute(
                        select(func.count())
                        .select_from(XUIInboundConnection)
                        .join(Inbound, Inbound.id == XUIInboundConnection.inbound_id)
                        .where(
                            XUIInboundConnection.subscription_id == subscription_id,
                            XUIInboundConnection.email == email,
                            XUIInboundConnection.id != connection.id,
                            # Email уникален лишь в рамках панели: одинаковая почта на
                            # другом сервере — это другой клиент, не «брат» по inbound'у.
                            Inbound.server_id == inbound.server_id,
                        )
                    )
                ).scalar() or 0
                if others > 0:
                    # Клиент остаётся на других inbound'ах — отвязываем только этот.
                    await provider.detach_inbounds(email, [getattr(inbound, "xui_id", inbound.id)])
                else:
                    await provider.remove_client(inbound, connection)
            else:
                await provider.remove_client(inbound, connection)
            if hasattr(inbound, "client_count") and inbound.client_count is not None:
                inbound.client_count -= 1

        # Delete from database. If the DB delete fails, the panel record is already
        # gone — mark the connection as error so the reconciler can clean it up later.
        try:
            await self.session.delete(connection)
            await self.session.flush()
        except Exception as db_error:
            # The panel removal succeeded but we can't purge the DB row right now.
            # Mark sync_status so the reconciler knows about the phantom row.
            try:
                connection.sync_status = "error"
                await self.session.flush()
            except Exception:
                pass
            logger.warning(
                "Panel client removed but DB delete failed for connection_id=%s "
                "inbound_id=%s subscription_id=%s error=%s",
                connection.id,
                inbound_id,
                subscription_id,
                db_error,
            )
            return False

        return True

    async def panel_extra_inbounds(self, subscription_id: int) -> list[dict]:
        """Найти на панели привязки XUI-клиентов подписки, которых нет в БД (ручные).

        Возвращает список словарей {server_id, email, extra_xui_ids} по каждому
        XUI-клиенту, у которого на панели есть inbound'ы вне БД. Пустой список —
        расхождения нет, полное удаление безопасно. Используется как pre-flight
        перед удалением, чтобы не снести молча ручные привязки.
        """
        from app.database.models.inbound import XUIInbound
        from app.services.xui_service import XUIService

        rows = (
            await self.session.execute(
                select(XUIInbound.server_id, XUIInboundConnection.email, XUIInbound.xui_id)
                .select_from(XUIInboundConnection)
                .join(XUIInbound, XUIInbound.id == XUIInboundConnection.inbound_id)
                .where(XUIInboundConnection.subscription_id == subscription_id)
            )
        ).all()
        if not rows:
            return []

        db_map: dict[tuple[int, str], set[int]] = {}
        for server_id, email, xui_id in rows:
            if email:
                db_map.setdefault((server_id, email), set()).add(xui_id)

        snapshots: dict[int, list] = {}
        result: list[dict] = []
        for (server_id, email), db_xui_ids in db_map.items():
            if server_id not in snapshots:
                server = await self.session.get(Server, server_id)
                if server is None:
                    snapshots[server_id] = []
                else:
                    try:
                        client = await XUIService(self.session)._get_client(server)
                        snapshots[server_id] = await client.get_clients() or []
                    except Exception as e:
                        logger.warning(
                            "panel_extra_inbounds: панель сервера {} недоступна: {}",
                            server_id, e,
                        )
                        snapshots[server_id] = []
            panel_ids: list[int] = []
            for pc in snapshots[server_id]:
                if (pc.get("email") or "") == email:
                    panel_ids = pc.get("inboundIds") or []
                    break
            extra = [x for x in panel_ids if x not in db_xui_ids]
            if extra:
                result.append(
                    {"server_id": server_id, "email": email, "extra_xui_ids": extra}
                )
        return result

    async def toggle_inbound_connection(
        self,
        connection_id: int,
        enable: bool,
    ) -> InboundConnection | None:
        """Enable or disable inbound connection.

        Args:
            connection_id: Connection ID
            enable: True to enable, False to disable

        Returns:
            Updated connection or None
        """
        conn_result = await self.session.execute(
            select(InboundConnection)
            .where(InboundConnection.id == connection_id)
            .options(
                selectinload(InboundConnection.inbound)
                .selectinload(Inbound.server)
                .selectinload(Server.xui_panel),
                selectinload(InboundConnection.inbound)
                .selectinload(Inbound.server)
                .selectinload(Server.awg_service),
                selectinload(InboundConnection.inbound)
                .selectinload(Inbound.server)
                .selectinload(Server.mtproxy_service),
                selectinload(InboundConnection.subscription).selectinload(Subscription.client)
            )
        )
        connection = conn_result.scalar_one_or_none()
        if not connection:
            return None

        inbound = connection.inbound
        provider = await self._get_provider(inbound.server, inbound=inbound)

        if inbound.type in ("awg_inbound", "mtproxy_inbound"):
            if enable:
                applied = await provider.enable_client(inbound, connection)
            else:
                applied = await provider.disable_client(inbound, connection)
            if not applied:
                # Флаг в БД не трогаем: иначе бот считает клиента отключённым,
                # а на сервере он продолжает работать.
                logger.warning(
                    "Сервер не подтвердил смену статуса connection {} — статус не изменён",
                    connection.id,
                )
                connection.sync_status = "error"
                await self.session.flush()
                return connection
        else:
            previous_enabled = connection.is_enabled
            connection.is_enabled = enable
            try:
                await provider.update_client(
                    inbound, connection, connection.total_gb, connection.expiry_date
                )
            except Exception as e:
                connection.is_enabled = previous_enabled
                connection.sync_status = "error"
                logger.warning(
                    "Не удалось переключить connection {} на панели: {}", connection.id, e
                )
                await self.session.flush()
                return connection

        connection.is_enabled = enable
        await self.session.flush()

        return connection

    async def toggle_client_all_connections(
        self,
        client_id: int,
        enable: bool,
    ) -> int:
        """Enable or disable all inbound connections for a client.

        Args:
            client_id: Client ID
            enable: True to enable, False to disable

        Returns:
            Number of connections toggled
        """
        # Get all subscriptions for client
        result = await self.session.execute(
            select(Subscription)
            .where(Subscription.client_id == client_id)
            .options(
                selectinload(Subscription.client),
                selectinload(Subscription.inbound_connections)
                .selectinload(InboundConnection.inbound)
                .selectinload(Inbound.server)
                .selectinload(Server.xui_panel),
                selectinload(Subscription.inbound_connections)
                .selectinload(InboundConnection.inbound)
                .selectinload(Inbound.server)
                .selectinload(Server.awg_service),
                selectinload(Subscription.inbound_connections)
                .selectinload(InboundConnection.inbound)
                .selectinload(Inbound.server)
                .selectinload(Server.mtproxy_service)
            )
        )
        subscriptions = result.scalars().all()

        toggled_count = 0
        for subscription in subscriptions:
            for connection in subscription.inbound_connections:
                inbound = connection.inbound
                provider = await self._get_provider(inbound.server, inbound=inbound)

                if inbound.type in ("awg_inbound", "mtproxy_inbound"):
                    if enable:
                        applied = await provider.enable_client(inbound, connection)
                    else:
                        applied = await provider.disable_client(inbound, connection)
                    if not applied:
                        logger.warning(
                            "Сервер не подтвердил смену статуса connection {} — статус не изменён",
                            connection.id,
                        )
                        connection.sync_status = "error"
                        continue
                else:
                    # Новый флаг выставляем до вызова: XUI-провайдер читает его
                    # из объекта. При отказе панели возвращаем прежний.
                    previous_enabled = connection.is_enabled
                    connection.is_enabled = enable
                    try:
                        await provider.update_client(
                            inbound, connection, connection.total_gb, connection.expiry_date
                        )
                    except Exception as e:
                        connection.is_enabled = previous_enabled
                        connection.sync_status = "error"
                        logger.warning(
                            "Не удалось переключить connection {} на панели: {}",
                            connection.id, e,
                        )
                        continue

                connection.is_enabled = enable
                toggled_count += 1

        await self.session.flush()
        return toggled_count

    async def delete_client_all_connections(self, client_id: int) -> int:
        """Delete all XUI clients for a client.

        Args:
            client_id: Client ID

        Returns:
            Number of connections deleted from XUI
        """
        # Get all subscriptions for client
        result = await self.session.execute(
            select(Subscription)
            .where(Subscription.client_id == client_id)
            .options(
                selectinload(Subscription.client),
                selectinload(Subscription.inbound_connections)
                .selectinload(InboundConnection.inbound)
                .selectinload(Inbound.server)
                .selectinload(Server.xui_panel),
                selectinload(Subscription.inbound_connections)
                .selectinload(InboundConnection.inbound)
                .selectinload(Inbound.server)
                .selectinload(Server.awg_service),
                selectinload(Subscription.inbound_connections)
                .selectinload(InboundConnection.inbound)
                .selectinload(Inbound.server)
                .selectinload(Server.mtproxy_service)
            )
        )
        subscriptions = result.scalars().all()

        deleted_count = 0
        # Один панельный клиент может быть общим для нескольких inbound'ов ОДНОЙ
        # панели (ключ — server_id+email), поэтому снимаем его один раз. На разных
        # панелях email может совпадать (он уникален лишь в рамках панели), поэтому
        # в ключ дедупа входит server_id — иначе на второй панели клиент останется
        # zombie. DB-строки соединений удаляем всегда, дедуп только пропускает
        # повторный вызов remove_client на той же панели.
        removed_keys: set[tuple] = set()
        for subscription in subscriptions:
            for connection in subscription.inbound_connections:
                # Delete from provider
                inbound = connection.inbound
                email = getattr(connection, "email", None)
                server_id = getattr(inbound, "server_id", None)
                key = (server_id, email)
                if email and key in removed_keys:
                    # Панельный клиент общий для нескольких inbound'ов панели — уже снят.
                    await self.session.delete(connection)
                    continue
                try:
                    provider = await self._get_provider(inbound.server, inbound=inbound)
                    await provider.remove_client(inbound, connection)
                    if email:
                        removed_keys.add(key)
                    deleted_count += 1
                except Exception as e:
                    logger.warning(
                        "delete_client_all_connections: не удалось удалить с панели connection_id={} "
                        "(email={}, inbound_id={}): {}. Клиент может остаться как zombie — "
                        "будет очищен реконсилятором.",
                        connection.id, getattr(connection, "email", "?"),
                        connection.inbound_id, e,
                    )
                # Always remove the DB record to prevent orphan rows,
                # even if the panel removal above failed.
                await self.session.delete(connection)

        await self.session.flush()
        return deleted_count

    async def sync_client_telegram_id(self, client_id: int) -> int:
        """Sync Telegram ID to all XUI clients for a client.

        Args:
            client_id: Client ID

        Returns:
            Number of connections updated in XUI
        """
        # Get client

        client = await self.session.get(Client, client_id)
        if not client:
            return 0

        # Get all subscriptions for client
        result = await self.session.execute(
            select(Subscription)
            .where(Subscription.client_id == client_id)
            .options(
                selectinload(Subscription.client),
                selectinload(Subscription.inbound_connections)
                .selectinload(InboundConnection.inbound)
                .selectinload(Inbound.server)
                .selectinload(Server.xui_panel),
                selectinload(Subscription.inbound_connections)
                .selectinload(InboundConnection.inbound)
                .selectinload(Inbound.server)
                .selectinload(Server.awg_service),
                selectinload(Subscription.inbound_connections)
                .selectinload(InboundConnection.inbound)
                .selectinload(Inbound.server)
                .selectinload(Server.mtproxy_service)
            )
        )
        subscriptions = result.scalars().all()

        updated_count = 0

        for subscription in subscriptions:
            for connection in subscription.inbound_connections:
                # Update tg_id in provider
                inbound = connection.inbound
                try:
                    provider = await self._get_provider(inbound.server, inbound=inbound)
                    # For XUI this works because it pulls latest from subscription.client.telegram_id
                    await provider.update_client(
                        inbound, connection, connection.total_gb, connection.expiry_date
                    )
                    updated_count += 1
                    logger.info(
                        "Telegram ID клиента {} обновлён в inbound {}",
                        client_id, inbound.id,
                    )

                except Exception as e:
                    logger.warning("Не удалось обновить Telegram ID для клиента {}: {}", client_id, e)

        return updated_count

    # Helper methods

    async def _get_provider(self, server: Any, inbound: Any = None) -> BaseVPNProvider:
        """Get or create VPN provider for server.

        Args:
            server: Server model
            inbound: Optional Inbound model (used to select correct provider
                     when server has multiple services)

        Returns:
            VPN provider instance
        """
        inbound_type = inbound.type if inbound else None
        cache_key = (server.id, inbound_type)
        if cache_key not in self._providers:
            provider = get_vpn_provider(server, inbound_type=inbound_type)
            if hasattr(provider, "_session"):
                provider._session = self.session
            self._providers[cache_key] = provider
        return self._providers[cache_key]

    async def close_all_clients(self) -> None:
        """Close all VPN providers properly."""
        for server_id in list(self._providers.keys()):
            provider = self._providers[server_id]
            try:
                await provider.close()
            except Exception as e:
                logger.warning("Ошибка при закрытии VPN-провайдера {}: {}", server_id, e)
            finally:
                self._providers.pop(server_id, None)

    # Subscription URLs

    async def get_subscription_urls(self, client_id: int) -> list[dict[str, Any]]:
        """Get all subscription URLs for client.

        Args:
            client_id: Client ID

        Returns:
            List of subscription info dicts
        """
        try:
            subscriptions = await self.get_client_subscriptions(client_id)

            urls = []
            for sub in subscriptions:
                seen_configs = set()
                for conn in sub.inbound_connections:
                    if not conn.is_enabled:
                        continue
                    try:
                        provider = await self._get_provider(conn.inbound.server, inbound=conn.inbound)
                        config = await provider.get_client_config(conn.inbound, conn)
                        config_data = config.get("config_data")
                        config_type = config.get("config_type")

                        if config_data and config_data not in seen_configs:
                            seen_configs.add(config_data)

                            # Only return links here, files are handled differently in UI
                            if config_type == "link":
                                urls.append(
                                    {
                                        "subscription_id": sub.id,
                                        "subscription_name": sub.name,
                                        "server_name": conn.inbound.server.name,
                                        "url": config_data,
                                        "token": sub.subscription_token,
                                        "type": "standard",
                                    }
                                )
                    except Exception as e:
                        from loguru import logger

                        logger.warning("Ошибка получения конфига для conn {}: {}", conn.id, e)

            return urls
        finally:
            await self.close_all_clients()

    async def get_subscription_json_urls(self, client_id: int) -> list[dict[str, Any]]:
        """Get all subscription JSON URLs for client."""
        try:
            subscriptions = await self.get_client_subscriptions(client_id)

            urls = []
            for sub in subscriptions:
                seen_configs = set()
                for conn in sub.inbound_connections:
                    if not conn.is_enabled:
                        continue
                    try:
                        provider = await self._get_provider(conn.inbound.server, inbound=conn.inbound)
                        config = await provider.get_client_config(
                            conn.inbound, conn, prefer_json=True
                        )
                        config_data = config.get("config_data")
                        config_type = config.get("config_type")

                        if config_data and config_data not in seen_configs:
                            seen_configs.add(config_data)

                            if config_type == "link":
                                urls.append(
                                    {
                                        "subscription_id": sub.id,
                                        "subscription_name": sub.name,
                                        "server_name": conn.inbound.server.name,
                                        "url": config_data,
                                        "token": sub.subscription_token,
                                        "type": "json",
                                    }
                                )
                    except Exception as e:
                        from loguru import logger

                        logger.warning("Ошибка получения JSON-конфига для conn {}: {}", conn.id, e)

            return urls
        finally:
            await self.close_all_clients()

    # Subscription management methods

    async def get_all_subscriptions(self) -> Sequence[Subscription]:
        """Get all subscriptions.

        Returns:
            List of all subscriptions
        """
        result = await self.session.execute(
            select(Subscription)
            .options(
                selectinload(Subscription.client),
                selectinload(Subscription.inbound_connections)
                .selectinload(InboundConnection.inbound)
                .selectinload(Inbound.server)
                .selectinload(Server.xui_panel),
                selectinload(Subscription.inbound_connections)
                .selectinload(InboundConnection.inbound)
                .selectinload(Inbound.server)
                .selectinload(Server.awg_service),
                selectinload(Subscription.inbound_connections)
                .selectinload(InboundConnection.inbound)
                .selectinload(Inbound.server)
                .selectinload(Server.mtproxy_service)
            )
            .order_by(Subscription.created_at.desc())
        )
        return result.scalars().all()

    async def update_subscription(
        self,
        subscription_id: int,
        name: str | None = None,
        total_gb: int | None = None,
        expiry_days: float | None = None,
        notes: str | None = None,
        is_active: bool | None = None,
        exact_expiry_date: datetime | None = None,
    ) -> Subscription:
        """Update subscription parameters.

        Args:
            subscription_id: Subscription ID
            name: New subscription name (optional)
            total_gb: New traffic limit in GB (optional)
            expiry_days: New expiry in days (optional, None = no change, 0 = never)
            notes: New notes (optional)
            is_active: New active status (optional)
            exact_expiry_date: Exact expiration date (optional)

        Returns:
            Updated subscription

        Raises:
            XUIError: If subscription not found
        """
        sub_result = await self.session.execute(
            select(Subscription)
            .where(Subscription.id == subscription_id)
            .options(selectinload(Subscription.client))
        )
        subscription = sub_result.scalar_one_or_none()
        if not subscription:
            raise XUIError("Subscription not found")

        # Update fields if provided
        if name is not None:
            subscription.name = name
        if total_gb is not None:
            subscription.total_gb = total_gb
        if exact_expiry_date is not None:
            subscription.expiry_date = exact_expiry_date
        elif expiry_days is not None:
            if expiry_days == 0:
                subscription.expiry_date = None
            else:
                subscription.expiry_date = datetime.now(UTC) + timedelta(days=expiry_days)
        if notes is not None:
            subscription.notes = notes
        if is_active is not None:
            subscription.is_active = is_active

        await self.session.flush()

        # Смена статуса тоже требует похода на сервер: без этого подписка
        # числится отключённой в БД, а VPN продолжает работать.
        if (
            total_gb is not None
            or expiry_days is not None
            or exact_expiry_date is not None
            or is_active is not None
        ):
            result = await self.session.execute(
                select(InboundConnection)
                .where(InboundConnection.subscription_id == subscription_id)
                .options(
                    selectinload(InboundConnection.inbound)
                    .selectinload(Inbound.server)
                    .selectinload(Server.xui_panel),
                    selectinload(InboundConnection.inbound)
                    .selectinload(Inbound.server)
                    .selectinload(Server.awg_service),
                    selectinload(InboundConnection.inbound)
                    .selectinload(Inbound.server)
                    .selectinload(Server.mtproxy_service),
                    selectinload(InboundConnection.subscription).selectinload(Subscription.client)
                )
            )
            connections = result.scalars().all()

            for connection in connections:
                try:
                    provider = await self._get_provider(connection.inbound.server, inbound=connection.inbound)

                    # У AWG/MTProxy update_client — заглушка, статусом управляют
                    # отдельные enable_client/disable_client. У XUI флаг enable
                    # едет внутри update_client.
                    if connection.inbound.type in ("awg_inbound", "mtproxy_inbound"):
                        if subscription.is_active:
                            applied = await provider.enable_client(connection.inbound, connection)
                        else:
                            applied = await provider.disable_client(connection.inbound, connection)
                        if not applied:
                            logger.warning(
                                "Сервер не подтвердил смену статуса connection {} — "
                                "оставляю прежний статус",
                                connection.id,
                            )
                            connection.sync_status = "error"
                            continue
                        connection.is_enabled = subscription.is_active
                    else:
                        # XUI-провайдер берёт enable из объекта, поэтому новое
                        # значение нужно выставить до вызова — и вернуть назад,
                        # если панель его не приняла.
                        previous_enabled = connection.is_enabled
                        connection.is_enabled = subscription.is_active
                        try:
                            await provider.update_client(
                                connection.inbound,
                                connection,
                                subscription.total_gb,
                                subscription.expiry_date,
                            )
                        except Exception:
                            connection.is_enabled = previous_enabled
                            raise

                    # Update per-connection settings
                    connection.total_gb = subscription.total_gb
                    connection.expiry_date = subscription.expiry_date
                    connection.sync_status = "synced"
                    connection.last_sync_at = datetime.now(UTC)
                except Exception as e:
                    logger.warning(
                        "Не удалось обновить VPN-клиент для connection {}: {}",
                        connection.id, e,
                    )
                    connection.sync_status = "error"

            await self.session.flush()

        # Reload with relationships
        updated = await self.get_subscription(subscription_id)
        if updated is None:
            raise XUIError("Subscription not found after update")
        return updated

    async def add_time_to_subscription(self, subscription_id: int, days: int) -> Subscription:
        """Add days to subscription expiry date.

        Args:
            subscription_id: Subscription ID
            days: Days to add

        Returns:
            Updated subscription

        Raises:
            XUIError: If subscription not found
        """
        sub_result = await self.session.execute(
            select(Subscription)
            .where(Subscription.id == subscription_id)
            .options(selectinload(Subscription.client))
        )
        subscription = sub_result.scalar_one_or_none()
        if not subscription:
            raise XUIError("Subscription not found")

        now = datetime.now(UTC)
        expiry = ensure_utc(subscription.expiry_date)

        if expiry is None or expiry < now:
            subscription.expiry_date = now + timedelta(days=days)
        else:
            subscription.expiry_date = expiry + timedelta(days=days)

        await self.session.flush()

        # Update XUI clients
        result = await self.session.execute(
            select(InboundConnection)
            .where(InboundConnection.subscription_id == subscription_id)
            .options(
                selectinload(InboundConnection.inbound)
                .selectinload(Inbound.server)
                .selectinload(Server.xui_panel),
                selectinload(InboundConnection.inbound)
                .selectinload(Inbound.server)
                .selectinload(Server.awg_service),
                selectinload(InboundConnection.inbound)
                .selectinload(Inbound.server)
                .selectinload(Server.mtproxy_service),
                selectinload(InboundConnection.subscription).selectinload(Subscription.client)
            )
        )
        connections = result.scalars().all()

        for connection in connections:
            try:
                provider = await self._get_provider(connection.inbound.server, inbound=connection.inbound)
                await provider.update_client(
                    connection.inbound, connection, subscription.total_gb, subscription.expiry_date
                )
                connection.expiry_date = subscription.expiry_date
                connection.sync_status = "synced"
                connection.last_sync_at = now

                if (
                    connection.inbound.type in ("awg_inbound", "mtproxy_inbound")
                    and not connection.is_enabled
                    and subscription.is_active
                ):
                    if await provider.enable_client(connection.inbound, connection):
                        connection.is_enabled = True
                    else:
                        # Продление без реального включения — худший случай для
                        # оплаты: деньги приняты, доступа нет, и бот это скрывает.
                        logger.warning(
                            "Сервер не подтвердил включение connection {} после продления",
                            connection.id,
                        )
                        connection.sync_status = "error"
            except Exception as e:
                logger.warning(
                    "Не удалось обновить VPN-клиент для connection {}: {}",
                    connection.id, e,
                )
                connection.sync_status = "error"

        await self.session.flush()
        updated = await self.get_subscription(subscription_id)
        if updated is None:
            raise XUIError("Subscription not found after update")
        return updated

    async def reset_subscription(self, subscription_id: int) -> bool:
        """Reset traffic for all connections in a subscription.

        Also resets the expiry date based on the template default or originally set duration.

        Args:
            subscription_id: Subscription ID

        Returns:
            True if successful

        Raises:
            XUIError: If subscription not found
        """
        self.session.expire_all()
        sub_result = await self.session.execute(
            select(Subscription)
            .where(Subscription.id == subscription_id)
            .options(selectinload(Subscription.client))
        )
        subscription = sub_result.scalar_one_or_none()
        if not subscription:
            raise XUIError("Subscription not found")

        now = datetime.now(UTC)

        # Calculate new expiry date
        base_days: int = 0
        if subscription.template_id and subscription.template:
            base_days = int(subscription.template.default_expiry_days or 0)
        else:
            if subscription.expiry_date:
                # Calculate original duration
                expiry = ensure_utc(subscription.expiry_date)
                created = ensure_utc(subscription.created_at)

                base_days = (expiry - created).days
                if base_days < 0:
                    base_days = 0

        if base_days > 0:
            subscription.expiry_date = now + timedelta(days=base_days)
            await self.session.flush()

        result = await self.session.execute(
            select(InboundConnection)
            .where(InboundConnection.subscription_id == subscription_id)
            .options(
                selectinload(InboundConnection.inbound)
                .selectinload(Inbound.server)
                .selectinload(Server.xui_panel),
                selectinload(InboundConnection.inbound)
                .selectinload(Inbound.server)
                .selectinload(Server.awg_service),
                selectinload(InboundConnection.inbound)
                .selectinload(Inbound.server)
                .selectinload(Server.mtproxy_service),
                selectinload(InboundConnection.subscription).selectinload(Subscription.client)
            )
        )
        connections = result.scalars().all()

        for connection in connections:
            try:
                provider = await self._get_provider(connection.inbound.server, inbound=connection.inbound)

                if base_days > 0:
                    await provider.update_client(
                        connection.inbound,
                        connection,
                        subscription.total_gb,
                        subscription.expiry_date,
                    )

                    connection.expiry_date = subscription.expiry_date
                    connection.sync_status = "synced"
                    connection.last_sync_at = now

                    if connection.inbound.type in ("awg_inbound", "mtproxy_inbound") and not connection.is_enabled:
                        await provider.enable_client(connection.inbound, connection)
                        connection.is_enabled = True

                await provider.reset_client_traffic(connection.inbound, connection)
            except Exception as e:
                logger.warning(
                    "Не удалось сбросить трафик VPN-клиента для connection {}: {}",
                    connection.id, e,
                )
                if base_days > 0:
                    connection.sync_status = "error"

        if base_days > 0:
            await self.session.flush()

        return True

    async def delete_subscription(self, subscription: Subscription | int) -> bool:
        """Delete subscription and all its inbound connections.

        Args:
            subscription: Subscription object (with loaded inbound_connections
                and inbound.server relations) or subscription ID

        Returns:
            True if deleted
        """
        if isinstance(subscription, int):
            self.session.expire_all()
            sub_result = await self.session.execute(
                select(Subscription)
                .where(Subscription.id == subscription)
                .options(
                    selectinload(Subscription.inbound_connections)
                    .selectinload(InboundConnection.inbound)
                    .selectinload(Inbound.server)
                    .selectinload(Server.xui_panel),
                    selectinload(Subscription.inbound_connections)
                    .selectinload(InboundConnection.inbound)
                    .selectinload(Inbound.server)
                    .selectinload(Server.awg_service),
                    selectinload(Subscription.inbound_connections)
                    .selectinload(InboundConnection.inbound)
                    .selectinload(Inbound.server)
                    .selectinload(Server.mtproxy_service),
                )
            )
            subscription = sub_result.scalar_one_or_none()
            if not subscription:
                return False

        # Один панельный клиент может быть общим для нескольких inbound'ов ОДНОЙ
        # панели (ключ — server_id+email), поэтому такого клиента снимаем один раз.
        # На разных панелях email может совпадать (он уникален лишь в рамках панели),
        # поэтому в ключ дедупа обязательно входит server_id — иначе на второй панели
        # клиент останется zombie.
        removed_keys: set[tuple] = set()
        for connection in subscription.inbound_connections:
            try:
                inbound = connection.inbound
                email = getattr(connection, "email", None)
                server_id = getattr(inbound, "server_id", None)
                key = (server_id, email)
                if email and key in removed_keys:
                    if inbound and hasattr(inbound, "client_count") and inbound.client_count is not None:
                        inbound.client_count -= 1
                    continue
                if inbound and inbound.server:
                    provider = await self._get_provider(inbound.server, inbound=inbound)
                    await provider.remove_client(inbound, connection)
                    if email:
                        removed_keys.add(key)
                    if hasattr(inbound, "client_count") and inbound.client_count is not None:
                        inbound.client_count -= 1
            except Exception as e:
                logger.warning(
                    "delete_subscription: не удалось удалить с панели connection_id={} "
                    "(email={}, inbound_id={}): {}. Клиент может остаться как zombie — "
                    "будет очищен реконсилятором.",
                    connection.id, getattr(connection, "email", "?"),
                    connection.inbound_id, e,
                )

        # Delete from database regardless of panel errors to keep the bot state consistent.
        await self.session.delete(subscription)
        await self.session.flush()
        return True

    async def release_known_inbounds_and_delete(
        self, subscription: Subscription | int
    ) -> bool:
        """Удалить подписку, но XUI-клиентов отвязать (detach), а не удалять целиком.

        Применяется, когда на панели у клиента есть привязки вне БД (ручные):
        detach снимает только БД-известные inbound'ы, сам клиент и его ручные
        привязки на панели сохраняются. AWG/MTProxy удаляются как обычно.
        """
        if isinstance(subscription, int):
            self.session.expire_all()
            sub_result = await self.session.execute(
                select(Subscription)
                .where(Subscription.id == subscription)
                .options(
                    selectinload(Subscription.inbound_connections)
                    .selectinload(InboundConnection.inbound)
                    .selectinload(Inbound.server)
                    .selectinload(Server.xui_panel),
                    selectinload(Subscription.inbound_connections)
                    .selectinload(InboundConnection.inbound)
                    .selectinload(Inbound.server)
                    .selectinload(Server.awg_service),
                    selectinload(Subscription.inbound_connections)
                    .selectinload(InboundConnection.inbound)
                    .selectinload(Inbound.server)
                    .selectinload(Server.mtproxy_service),
                )
            )
            subscription = sub_result.scalar_one_or_none()
            if not subscription:
                return False

        # XUI: группируем по (server_id, email) и отвязываем БД-известные xui_id.
        xui_groups: dict[tuple, dict] = {}
        other_conns: list = []
        for connection in subscription.inbound_connections:
            inbound = connection.inbound
            email = getattr(connection, "email", None)
            if getattr(inbound, "type", None) == "xui_inbound" and email:
                key = (getattr(inbound, "server_id", None), email)
                grp = xui_groups.setdefault(
                    key, {"server": inbound.server, "inbound": inbound, "xui_ids": []}
                )
                grp["xui_ids"].append(getattr(inbound, "xui_id", inbound.id))
            else:
                other_conns.append(connection)

        for (server_id, email), grp in xui_groups.items():
            try:
                if grp["server"] is not None:
                    provider = await self._get_provider(grp["server"], inbound=grp["inbound"])
                    await provider.detach_inbounds(email, grp["xui_ids"])
            except Exception as e:
                logger.warning(
                    "release_known: не удалось отвязать {} от {} (сервер {}): {}",
                    email, grp["xui_ids"], server_id, e,
                )

        # AWG/MTProxy: обычное удаление с сервера.
        for connection in other_conns:
            try:
                inbound = connection.inbound
                if inbound and inbound.server:
                    provider = await self._get_provider(inbound.server, inbound=inbound)
                    await provider.remove_client(inbound, connection)
            except Exception as e:
                logger.warning(
                    "release_known: не удалось удалить не-XUI connection {}: {}",
                    connection.id, e,
                )

        await self.session.delete(subscription)
        await self.session.flush()
        return True

    async def get_subscription_inbounds(self, subscription_id: int) -> Sequence[InboundConnection]:
        """Get all inbound connections for subscription.

        Args:
            subscription_id: Subscription ID

        Returns:
            List of inbound connections
        """
        result = await self.session.execute(
            select(InboundConnection)
            .where(InboundConnection.subscription_id == subscription_id)
            .options(
                selectinload(InboundConnection.inbound).selectinload(Inbound.server).selectinload(Server.xui_panel),
                selectinload(InboundConnection.inbound).selectinload(Inbound.server).selectinload(Server.awg_service),
                selectinload(InboundConnection.inbound).selectinload(Inbound.server).selectinload(Server.mtproxy_service),
            )
            .order_by(InboundConnection.created_at.desc())
        )
        return result.scalars().all()

    async def get_subscription_by_id(self, subscription_id: int) -> Subscription | None:
        """Get subscription by ID with full relations.

        Expires cached state first to ensure fresh data from DB,
        including all eagerly loaded relationships.

        Args:
            subscription_id: Subscription ID

        Returns:
            Subscription or None
        """
        self.session.expire_all()
        return await self.get_subscription(subscription_id)
