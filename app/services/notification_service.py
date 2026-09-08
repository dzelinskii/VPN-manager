"""Notification service for sending Telegram notifications."""

import html

from aiogram import Bot
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database.models import Client, InboundConnection, Subscription

# Module-level singleton Bot — shared across all NotificationService instances.
# Created lazily on first call (Bot binds to the running event loop).
_shared_bot: Bot | None = None


async def _get_shared_bot() -> Bot:
    """Return the module-level singleton Bot, creating it lazily if needed."""
    global _shared_bot
    if _shared_bot is None:
        _shared_bot = Bot(token=get_settings().bot_token)
    return _shared_bot


async def close_shared_bot() -> None:
    """Close the singleton Bot's HTTP session and reset the cache.

    Call this on graceful shutdown to eliminate 'Unclosed client session' warnings.
    After calling, the next _get_shared_bot() call will create a fresh instance.
    """
    global _shared_bot
    if _shared_bot is not None:
        try:
            await _shared_bot.session.close()
        finally:
            _shared_bot = None


class NotificationService:
    """Service for sending notifications to clients."""

    @staticmethod
    def _build_subscription_links(
        connections: list[InboundConnection], subscription: Subscription
    ) -> str:
        """Build subscription URL/config section for notifications.

        Returns text with XUI URLs and/or config download hint for AWG/MTProxy.
        """
        if not connections:
            return ""

        from urllib.parse import urljoin

        xui_servers = {
            conn.inbound.server
            for conn in connections
            if getattr(conn.inbound.server, "xui_panel", None)
        }
        awg_protocols = set()
        mtproxy_links = []
        for conn in connections:
            if getattr(conn.inbound.server, "xui_panel", None):
                continue
            if conn.inbound.type == "mtproxy_inbound":
                svc = conn.inbound.server.mtproxy_service
                secret = getattr(conn, "secret", None) or (svc.default_secret if svc else None)
                port = svc.port if svc else 443
                host = conn.inbound.server.ip_address
                if secret:
                    mtproxy_links.append(
                        f"📡 <b>{html.escape(conn.inbound.remark)}</b>:\n"
                        f"<a href=\"tg://proxy?server={host}&port={port}&secret={secret}\">Подключить MTProxy</a>\n"
                        f"<code>tg://proxy?server={host}&port={port}&secret={secret}</code>"
                    )
            else:
                awg_protocols.add(conn.inbound.protocol)

        parts = []
        if xui_servers:
            urls = []
            for server in xui_servers:
                subscription_path = getattr(server.xui_panel, "subscription_json_path", None)
                if not subscription_path:
                    subscription_path = getattr(server.xui_panel, "subscription_path", "/sub/")
                if not subscription_path:
                    subscription_path = "/sub/"

                server_url = (
                    server.ip_address
                    if server.ip_address and server.ip_address.startswith("http")
                    else f"http://{server.ip_address}"
                )
                urls.append(
                    urljoin(server_url, f"{subscription_path}{subscription.subscription_token}")
                )
            parts.append("🔗 <b>URL подписки:</b>\n" + "\n\n".join(f"<code>{u}</code>" for u in urls))

        if awg_protocols:
            protocols_str = ", ".join(sorted(awg_protocols))
            parts.append(
                f"📁 <b>Конфиг:</b> {protocols_str} — "
                "скачайте конфигурационный файл в разделе подписок"
            )

        if mtproxy_links:
            parts.append("🔗 <b>MTProxy:</b>\n" + "\n".join(mtproxy_links))

        return "\n" + "\n".join(parts) if parts else ""

    def __init__(self, session: AsyncSession) -> None:
        """Initialize service with database session.

        Args:
            session: Async database session
        """
        self.session = session
        self.bot_token = get_settings().bot_token

    async def _get_bot(self) -> Bot:
        """Return the shared singleton Bot instance.

        Returns:
            Singleton Bot instance (created lazily on first call).
        """
        return await _get_shared_bot()

    async def notify_subscription_created(
        self,
        client: Client,
        subscription: Subscription,
        connections: list[InboundConnection],
    ) -> bool:
        """Send notification when subscription is created.

        Args:
            client: Client that received subscription
            subscription: Created subscription
            connections: List of created inbound connections

        Returns:
            True if notification sent, False if not (no telegram_id)
        """
        if not client.telegram_id:
            return False

        try:
            bot = await self._get_bot()
            safe_client_name = html.escape(client.name) if client.name else "Не указан"
            safe_sub_name = (
                html.escape(subscription.name) if subscription.name else "Не указана"
            )

            # Build message
            message = (
                f"🎉 <b>Новая подписка создана!</b>\n\n"
                f"👤 <b>Клиент:</b> {safe_client_name}\n"
                f"📦 <b>Подписка:</b> {safe_sub_name}\n\n"
                f"<b>Подключения:</b>\n"
            )

            for i, conn in enumerate(connections, 1):
                inbound = conn.inbound
                server = inbound.server
                status = "✅" if conn.is_enabled else "❌"
                safe_remark = html.escape(inbound.remark) if inbound.remark else "Без названия"
                safe_server_name = html.escape(server.name) if server.name else "Неизвестный"
                message += (
                    f"{i}. {status} <b>{safe_remark}</b>\n"
                    f"   Сервер: {safe_server_name}\n"
                    f"   Протокол: {inbound.protocol}\n"
                )

            # Add subscription details
            traffic_limit = (
                f"{subscription.total_gb} ГБ" if subscription.total_gb > 0 else "Безлимитный"
            )
            expiry_text = (
                f"{subscription.remaining_days} дн."
                if subscription.expiry_date
                else "Бессрочная"
            )

            message += (
                f"\n📊 <b>Лимит трафика:</b> {traffic_limit}\n"
                f"📅 <b>Срок действия:</b> {expiry_text}\n"
            )

            message += self._build_subscription_links(connections, subscription)

            await bot.send_message(
                chat_id=client.telegram_id,
                text=message,
                parse_mode="HTML",
            )

            logger.info(
                "Уведомление о создании подписки отправлено клиенту {} (telegram_id={}), подписка {}",
                client.name, client.telegram_id, subscription.name,
            )

            return True

        except Exception as e:
            logger.error(
                "Не удалось отправить уведомление о создании клиенту {} (telegram_id={}): {}",
                client.name, client.telegram_id, e,
                exc_info=True,
            )
            return False

    async def notify_subscription_updated(
        self,
        client: Client,
        subscription: Subscription,
    ) -> bool:
        """Send notification when subscription is updated.

        Args:
            client: Client that owns subscription
            subscription: Updated subscription

        Returns:
            True if notification sent, False if not (no telegram_id)
        """
        if not client.telegram_id:
            return False

        try:
            bot = await self._get_bot()
            safe_client_name = html.escape(client.name) if client.name else "Не указан"
            safe_sub_name = (
                html.escape(subscription.name) if subscription.name else "Не указана"
            )

            # Build message
            message = (
                f"🔄 <b>Подписка обновлена!</b>\n\n"
                f"👤 <b>Клиент:</b> {safe_client_name}\n"
                f"📦 <b>Подписка:</b> {safe_sub_name}\n"
                f"✅ <b>Статус:</b> {'Активна' if subscription.is_active else 'Отключена'}\n"
            )

            # Add subscription details
            traffic_limit = (
                f"{subscription.total_gb} ГБ" if subscription.total_gb > 0 else "Безлимитный"
            )
            expiry_text = (
                f"{subscription.remaining_days} дн."
                if subscription.expiry_date
                else "Бессрочная"
            )

            message += (
                f"📊 <b>Лимит трафика:</b> {traffic_limit}\n"
                f"📅 <b>Срок действия:</b> {expiry_text}\n"
            )

            connections = getattr(subscription, "inbound_connections", [])
            message += self._build_subscription_links(connections, subscription)

            await bot.send_message(
                chat_id=client.telegram_id,
                text=message,
                parse_mode="HTML",
            )

            logger.info(
                "Уведомление об обновлении подписки отправлено клиенту {} (telegram_id={}), подписка {}",
                client.name, client.telegram_id, subscription.name,
            )

            return True

        except Exception as e:
            logger.error(
                "Не удалось отправить уведомление об обновлении клиенту {} (telegram_id={}): {}",
                client.name, client.telegram_id, e,
                exc_info=True,
            )
            return False

    async def notify_subscription_rebuilt(
        self,
        client: Client,
        subscription: Subscription,
        old_name: str,
    ) -> bool:
        """Send notification when subscription is rebuilt (reused token)."""
        if not client.telegram_id:
            return False

        try:
            bot = await self._get_bot()
            safe_old_name = html.escape(old_name) if old_name else "Не указана"
            safe_new_name = (
                html.escape(subscription.name) if subscription.name else "Не указана"
            )

            from app.utils.texts import t

            if safe_old_name == safe_new_name:
                message = t(
                    "notifications.rebuilt_same_name",
                    "🎉 Ваша подписка <b>{name}</b> обновлена!",
                    name=safe_new_name,
                )
            else:
                message = t(
                    "notifications.rebuilt_diff_name",
                    "🎉 Ваша подписка <b>{old_name}</b> изменена на <b>{new_name}</b>!",
                    old_name=safe_old_name,
                    new_name=safe_new_name,
                )

            traffic_limit = (
                f"{subscription.total_gb} ГБ"
                if subscription.total_gb > 0
                else t("admin.templates.unlimited_capital", "Безлимитный")
            )
            expiry_text = (
                f"{subscription.remaining_days} дн."
                if subscription.expiry_date
                else t("admin.templates.unlimited_time_capital", "Бессрочная")
            )

            message += t(
                "notifications.rebuilt_details",
                "\n\n📊 <b>Новый лимит трафика:</b> {traffic}\n📅 <b>Новый срок действия:</b> {expiry}",
                traffic=traffic_limit,
                expiry=expiry_text,
            )

            await bot.send_message(
                chat_id=client.telegram_id,
                text=message,
                parse_mode="HTML",
            )

            logger.info("Уведомление о перестройке подписки отправлено клиенту {}", client.name)

            return True

        except Exception as e:
            logger.error(
                "Не удалось отправить уведомление о перестройке клиенту {}: {}",
                client.name, e,
            )
            return False

    async def notify_subscription_deleted(
        self,
        client: Client,
        subscription_name: str,
    ) -> bool:
        """Send notification when subscription is deleted.

        Args:
            client: Client that owned subscription
            subscription_name: Name of deleted subscription

        Returns:
            True if notification sent, False if not (no telegram_id)
        """
        if not client.telegram_id:
            return False

        try:
            bot = await self._get_bot()
            safe_client_name = html.escape(client.name) if client.name else "Не указан"
            safe_sub_name = (
                html.escape(subscription_name) if subscription_name else "Не указана"
            )

            message = (
                f"❌ <b>Подписка удалена</b>\n\n"
                f"👤 <b>Клиент:</b> {safe_client_name}\n"
                f"📦 <b>Подписка:</b> {safe_sub_name}\n\n"
                f"Если это ошибка, обратитесь к администратору."
            )

            await bot.send_message(
                chat_id=client.telegram_id,
                text=message,
                parse_mode="HTML",
            )

            logger.info(
                "Уведомление об удалении подписки отправлено клиенту {} (telegram_id={}), подписка {}",
                client.name, client.telegram_id, subscription_name,
            )

            return True

        except Exception as e:
            logger.error(
                "Не удалось отправить уведомление об удалении клиенту {} (telegram_id={}): {}",
                client.name, client.telegram_id, e,
                exc_info=True,
            )
            return False

    async def notify_inbound_added(
        self,
        client: Client,
        subscription: Subscription,
        connection: InboundConnection,
    ) -> bool:
        """Send notification when inbound is added to subscription.

        Args:
            client: Client that owns subscription
            subscription: Subscription
            connection: Added inbound connection

        Returns:
            True if notification sent, False if not (no telegram_id)
        """
        if not client.telegram_id:
            return False

        try:
            bot = await self._get_bot()
            inbound = connection.inbound
            server = inbound.server

            safe_client_name = html.escape(client.name) if client.name else "Не указан"
            safe_sub_name = (
                html.escape(subscription.name) if subscription.name else "Не указана"
            )
            safe_remark = html.escape(inbound.remark) if inbound.remark else "Без названия"
            safe_server_name = html.escape(server.name) if server.name else "Неизвестный"

            message = (
                f"➕ <b>Новое подключение добавлено!</b>\n\n"
                f"👤 <b>Клиент:</b> {safe_client_name}\n"
                f"📦 <b>Подписка:</b> {safe_sub_name}\n\n"
                f"🔌 <b>Подключение:</b> {safe_remark}\n"
                f"🖥️ <b>Сервер:</b> {safe_server_name}\n"
                f"⚙️ <b>Протокол:</b> {inbound.protocol}\n"
                f"📡 <b>Порт:</b> {inbound.port}\n"
            )

            message += self._build_subscription_links([connection], subscription)

            await bot.send_message(
                chat_id=client.telegram_id,
                text=message,
                parse_mode="HTML",
            )

            logger.info(
                "Уведомление о добавлении подключения отправлено клиенту {} (telegram_id={}), подписка {}",
                client.name, client.telegram_id, subscription.name,
            )

            return True

        except Exception as e:
            logger.error(
                "Не удалось отправить уведомление о добавлении подключения клиенту {} (telegram_id={}): {}",
                client.name, client.telegram_id, e,
                exc_info=True,
            )
            return False

    async def notify_inbound_removed(
        self,
        client: Client,
        subscription_name: str,
        inbound_remark: str,
    ) -> bool:
        """Send notification when inbound is removed from subscription.

        Args:
            client: Client that owns subscription
            subscription_name: Subscription name
            inbound_remark: Inbound remark that was removed

        Returns:
            True if notification sent, False if not (no telegram_id)
        """
        if not client.telegram_id:
            return False

        try:
            bot = await self._get_bot()
            safe_client_name = html.escape(client.name) if client.name else "Не указан"
            safe_sub_name = (
                html.escape(subscription_name) if subscription_name else "Не указана"
            )
            safe_remark = html.escape(inbound_remark) if inbound_remark else "Без названия"

            message = (
                f"➖ <b>Подключение удалено</b>\n\n"
                f"👤 <b>Клиент:</b> {safe_client_name}\n"
                f"📦 <b>Подписка:</b> {safe_sub_name}\n"
                f"🔌 <b>Удалено подключение:</b> {safe_remark}\n\n"
                f"Если это ошибка, обратитесь к администратору."
            )

            await bot.send_message(
                chat_id=client.telegram_id,
                text=message,
                parse_mode="HTML",
            )

            logger.info(
                "Уведомление об удалении подключения отправлено клиенту {} (telegram_id={}), подписка {}",
                client.name, client.telegram_id, subscription_name,
            )

            return True

        except Exception as e:
            logger.error(
                "Не удалось отправить уведомление об удалении подключения клиенту {} (telegram_id={}): {}",
                client.name, client.telegram_id, e,
                exc_info=True,
            )
            return False

    async def notify_expiry_warning(
        self,
        client: Client,
        notification_type: str,
        message: str,
    ) -> bool:
        """Send expiry warning notification.

        Args:
            client: Client to notify
            notification_type: Type of expiry warning (24h, 12h, 1h)
            message: Formatted message

        Returns:
            True if notification sent, False if not (no telegram_id)
        """
        if not client.telegram_id:
            return False

        try:
            bot = await self._get_bot()
            await bot.send_message(
                chat_id=client.telegram_id,
                text=message,
                parse_mode="HTML",
            )

            logger.info(
                "Предупреждение об истечении отправлено клиенту {} (telegram_id={}), тип {}",
                client.name, client.telegram_id, notification_type,
            )

            return True

        except Exception as e:
            logger.error(
                "Не удалось отправить предупреждение об истечении клиенту {} (telegram_id={}): {}",
                client.name, client.telegram_id, e,
                exc_info=True,
            )
            return False

    async def notify_traffic_warning(
        self,
        client: Client,
        message: str,
    ) -> bool:
        """Send traffic warning notification.

        Args:
            client: Client to notify
            message: Formatted message

        Returns:
            True if notification sent, False if not (no telegram_id)
        """
        if not client.telegram_id:
            return False

        try:
            bot = await self._get_bot()
            await bot.send_message(
                chat_id=client.telegram_id,
                text=message,
                parse_mode="HTML",
            )

            logger.info(
                "Предупреждение о трафике отправлено клиенту {} (telegram_id={})",
                client.name, client.telegram_id,
            )

            return True

        except Exception as e:
            logger.error(
                "Не удалось отправить предупреждение о трафике клиенту {} (telegram_id={}): {}",
                client.name, client.telegram_id, e,
                exc_info=True,
            )
            return False

    async def notify_admin_of_new_user(self, client: Client) -> None:
        """Send notification to admins about new user registration.

        Args:
            client: The newly registered client
        """
        settings = get_settings()
        admin_ids = settings.admin_ids
        if not admin_ids:
            logger.warning("Нет admin_ids — пропуск уведомления о новом пользователе")
            return

        safe_name = html.escape(client.name) if client.name else "Не указан"
        safe_email = html.escape(client.email) if client.email else "Не указан"

        message = (
            f"👤 <b>Новый пользователь зарегистрирован!</b>\n\n"
            f"<b>ID:</b> {client.id}\n"
            f"<b>Имя:</b> {safe_name}\n"
            f"<b>Telegram ID:</b> {client.telegram_id}\n"
            f"<b>Email:</b> {safe_email}"
        )

        try:
            bot = await self._get_bot()
            for admin_id in admin_ids:
                try:
                    await bot.send_message(
                        chat_id=admin_id,
                        text=message,
                        parse_mode="HTML",
                    )
                    logger.info(
                        "Уведомление администратору {} о новом пользователе {}",
                        admin_id, safe_name,
                    )
                except Exception as e:
                    logger.warning(
                        "Не удалось отправить уведомление администратору {} о новом пользователе {}: {}",
                        admin_id, safe_name, e,
                    )
        except Exception as e:
            logger.error(
                "Ошибка при отправке уведомлений администраторам о новом пользователе {}: {}",
                client.name, e,
                exc_info=True,
            )

    async def notify_admin_of_subscription_request(
        self, client: Client, comment: str | None = None
    ) -> None:
        """Send notification to admins about a user requesting a new subscription.

        Args:
            client: The client requesting the subscription
            comment: Optional comment from the user
        """
        settings = get_settings()
        admin_ids = settings.admin_ids
        if not admin_ids:
            logger.warning("Нет admin_ids — пропуск уведомления о запросе подписки")
            return

        safe_name = html.escape(client.name) if client.name else "Не указан"

        message = (
            f"🔔 <b>Запрос на новую подписку!</b>\n\n"
            f"<b>Клиент:</b> {safe_name} (ID: {client.id})\n"
            f"<b>Telegram ID:</b> {client.telegram_id}"
        )

        if comment:
            safe_comment = html.escape(comment)
            message += f"\n<b>Комментарий:</b> {safe_comment}"

        try:
            bot = await self._get_bot()
            for admin_id in admin_ids:
                try:
                    await bot.send_message(
                        chat_id=admin_id,
                        text=message,
                        parse_mode="HTML",
                    )
                    logger.info(
                        "Уведомление администратору {} о запросе подписки от {}",
                        admin_id, safe_name,
                    )
                except Exception as e:
                    logger.warning(
                        "Не удалось отправить уведомление администратору {} о запросе от {}: {}",
                        admin_id, safe_name, e,
                    )
        except Exception as e:
            logger.error(
                "Ошибка при отправке уведомлений администраторам о запросе от {}: {}",
                client.name, e,
                exc_info=True,
            )

    async def notify_admins_new_request(self, request, template_name: str) -> None:
        """Send notification to admins about a specific subscription request.

        Args:
            request: SubscriptionRequest instance
            template_name: The name of the requested template
        """
        settings = get_settings()
        admin_ids = settings.admin_ids
        if not admin_ids:
            logger.warning("Нет admin_ids — пропуск уведомления о новом запросе")
            return

        from app.bot.keyboards.inline import get_request_admin_keyboard

        safe_name = html.escape(request.client.name) if request.client.name else "Не указан"
        safe_tpl_name = html.escape(template_name)
        safe_req_name = (
            html.escape(request.requested_name) if request.requested_name else "Не указано"
        )

        message = (
            f"🔔 <b>Новый запрос на подписку!</b>\n\n"
            f"<b>Клиент:</b> {safe_name} (ID: {request.client_id})\n"
            f"<b>Шаблон:</b> {safe_tpl_name}\n"
            f"<b>Название:</b> {safe_req_name}"
        )

        keyboard = get_request_admin_keyboard(request.id)

        try:
            bot = await self._get_bot()
            for admin_id in admin_ids:
                try:
                    await bot.send_message(
                        chat_id=admin_id,
                        text=message,
                        parse_mode="HTML",
                        reply_markup=keyboard,
                    )
                except Exception as e:
                    logger.warning(
                        "Не удалось отправить уведомление о запросе администратору {}: {}",
                        admin_id, e,
                    )
        except Exception as e:
            logger.error(
                "Ошибка при отправке уведомлений администраторам о запросе {}: {}",
                request.id, e,
            )

    async def notify_user_request_decision(
        self,
        telegram_id: int,
        is_approved: bool,
        sub_name: str | None = None,
        template_name: str | None = None,
    ) -> bool:
        """Send notification to user about the decision on their request.

        Args:
            telegram_id: User's telegram ID
            is_approved: Whether the request was approved
            sub_name: The name of the subscription
            template_name: The name of the template

        Returns:
            True if notification sent, False if not
        """
        if not telegram_id:
            return False

        try:
            bot = await self._get_bot()
            if is_approved:
                message = "✅ <b>Ваш запрос одобрен!</b>"
                if sub_name:
                    safe_name = html.escape(sub_name)
                    message += f"\nПодписка <b>{safe_name}</b> была создана."
            else:
                safe_sub = (
                    html.escape(sub_name)
                    if sub_name and sub_name != "Не указано"
                    else "без названия"
                )
                safe_tpl = html.escape(template_name) if template_name else "неизвестно"
                message = f"❌ Ваш запрос на создание подписки <b>{safe_sub}</b> по шаблону <b>{safe_tpl}</b> был отклонен."

            await bot.send_message(
                chat_id=telegram_id,
                text=message,
                parse_mode="HTML",
            )
            return True
        except Exception as e:
            logger.error(
                "Не удалось отправить уведомление о решении по запросу пользователю {}: {}",
                telegram_id, e,
            )
            return False

    async def notify_admins_missing_on_panel(
        self,
        server_name: str,
        marked_connections: list[dict],
    ) -> None:
        """Уведомить администраторов о клиентах, пропавших с панели (вручную удалённых).

        Args:
            server_name: Имя сервера, на котором обнаружены пропавшие клиенты.
            marked_connections: Список словарей с ключами 'email' и 'user'.
        """
        settings = get_settings()
        admin_ids = settings.admin_ids
        if not admin_ids:
            logger.warning("Нет admin_ids — пропуск уведомления об удалённых клиентах")
            return

        if not marked_connections:
            return

        lines = []
        for entry in marked_connections:
            email = html.escape(entry.get("email") or "—")
            user = html.escape(entry.get("user") or "—")
            lines.append(f"• <code>{email}</code> (пользователь: {user})")

        safe_server = html.escape(server_name)
        body = "\n".join(lines)
        message = (
            f"⚠️ <b>Клиенты удалены с панели вручную</b>\n\n"
            f"<b>Сервер:</b> {safe_server}\n\n"
            f"Следующие соединения отсутствуют в снимке панели и помечены "
            f"как <code>error</code> для последующей очистки:\n\n"
            f"{body}"
        )

        try:
            bot = await self._get_bot()
            for admin_id in admin_ids:
                try:
                    await bot.send_message(
                        chat_id=admin_id,
                        text=message,
                        parse_mode="HTML",
                    )
                    logger.info(
                        "Уведомление об удалённых клиентах отправлено администратору {} (сервер {})",
                        admin_id, server_name,
                    )
                except Exception as e:
                    logger.warning(
                        "Не удалось отправить уведомление об удалённых клиентах администратору {}: {}",
                        admin_id, e,
                    )
        except Exception as e:
            logger.error(
                "Ошибка отправки уведомлений об удалённых клиентах (сервер {}): {}",
                server_name, e,
                exc_info=True,
            )

    async def notify_admins_stuck_pending_push(
        self,
        server_name: str,
        stuck_connections: list[dict],
    ) -> None:
        """Уведомить администраторов о неотправленных изменениях без клиента на панели.

        Отличается от :meth:`notify_admins_missing_on_panel` тем, что здесь
        автоматической очистки не будет: изменение записано в БД, а применить
        его некуда — клиента на панели нет. Нужны руки.

        Args:
            server_name: Имя сервера.
            stuck_connections: Список словарей с ключами 'email' и 'user'.
        """
        settings = get_settings()
        admin_ids = settings.admin_ids
        if not admin_ids:
            logger.warning("Нет admin_ids — пропуск уведомления о застрявших изменениях")
            return

        if not stuck_connections:
            return

        lines = []
        for entry in stuck_connections:
            email = html.escape(entry.get("email") or "—")
            user = html.escape(entry.get("user") or "—")
            lines.append(f"• <code>{email}</code> (пользователь: {user})")

        message = (
            f"⚠️ <b>Изменения не удалось применить</b>\n\n"
            f"<b>Сервер:</b> {html.escape(server_name)}\n\n"
            f"Для этих подписок изменение записано в базе, но клиента нет на "
            f"панели — досылка невозможна, автоматически это не разрешится:\n\n"
            f"{chr(10).join(lines)}\n\n"
            f"Восстанавливайте через бота — «Пересобрать» или пересоздание "
            f"подписки. Если завести клиента руками в панели, у него будет "
            f"другой UUID, и бот его не подхватит."
        )

        try:
            bot = await self._get_bot()
            for admin_id in admin_ids:
                try:
                    await bot.send_message(
                        chat_id=admin_id, text=message, parse_mode="HTML"
                    )
                except Exception as e:
                    logger.warning(
                        "Не удалось уведомить администратора {} о застрявших изменениях: {}",
                        admin_id, e,
                    )
        except Exception as e:
            logger.error(
                "Ошибка отправки уведомлений о застрявших изменениях (сервер {}): {}",
                server_name, e, exc_info=True,
            )

    # === Расхождения БД ↔ панель (governed reconcile) ===

    @staticmethod
    def _build_divergence_digest_text(server_name: str, pendings: list, is_mass: bool) -> str:
        """Собрать текст дайджеста расхождений из списка pending-записей."""
        from app.services.divergence_service import (
            KIND_EXTRA,
            KIND_MISSING,
            STATUS_OPEN,
        )

        open_missing = [p for p in pendings if p.kind == KIND_MISSING and p.status == STATUS_OPEN]
        open_extra = [p for p in pendings if p.kind == KIND_EXTRA and p.status == STATUS_OPEN]
        resolved = [p for p in pendings if p.status != STATUS_OPEN]
        open_count = len(open_missing) + len(open_extra)

        safe_server = html.escape(server_name)
        lines = [f"⚠️ <b>Расхождения БД ↔ панель — сервер {safe_server}</b>"]
        if is_mass:
            lines.append("⚡ <b>Возможен массовый сбой</b> (откат/потеря панели) — авто-удаление заблокировано")
        lines.append("")

        def _names(items: list) -> str:
            shown = [f"   • <code>{html.escape(p.email)}</code>" for p in items[:3]]
            if len(items) > 3:
                shown.append(f"   • …ещё {len(items) - 3}")
            return "\n".join(shown)

        if open_missing:
            lines.append(f"📉 <b>Пропали с панели:</b> {len(open_missing)}")
            lines.append(_names(open_missing))
        if open_extra:
            lines.append(f"📈 <b>Лишнее на панели:</b> {len(open_extra)}")
            lines.append(_names(open_extra))

        if resolved:
            lines.append("")
            lines.append(f"✅ Разрешено: {len(resolved)}, осталось: {open_count}")

        if open_count == 0:
            lines.append("")
            lines.append("✅ Все расхождения разрешены.")

        return "\n".join(lines)

    async def notify_admins_divergences(
        self, server_name: str, batch_id: str, pendings: list, is_mass: bool
    ) -> None:
        """Отправить администраторам дайджест расхождений с кнопками (режим ask).

        Сохраняет id разосланных сообщений в notify_message_refs каждого pending
        батча — чтобы потом редактировать дайджест по мере решений.
        """
        from app.bot.keyboards.inline import get_divergence_digest_keyboard

        admin_ids = get_settings().admin_ids
        if not admin_ids or not pendings:
            return

        text = self._build_divergence_digest_text(server_name, pendings, is_mass)
        keyboard = get_divergence_digest_keyboard(batch_id, open_count=len(pendings))

        refs: list[list[int]] = []
        bot = await self._get_bot()
        for admin_id in admin_ids:
            try:
                msg = await bot.send_message(
                    chat_id=admin_id, text=text, parse_mode="HTML", reply_markup=keyboard
                )
                refs.append([admin_id, msg.message_id])
            except Exception as e:
                logger.warning("Не удалось отправить дайджест расхождений админу {}: {}", admin_id, e)

        for pd in pendings:
            pd.notify_message_refs = refs
        await self.session.flush()

    async def notify_admins_divergences_report(
        self, server_name: str, findings: list
    ) -> None:
        """Отправить администраторам сводку расхождений без кнопок (режим report)."""
        from app.services.divergence_service import KIND_EXTRA, KIND_MISSING

        admin_ids = get_settings().admin_ids
        if not admin_ids or not findings:
            return

        missing = [f for f in findings if f.kind == KIND_MISSING]
        extra = [f for f in findings if f.kind == KIND_EXTRA]
        safe_server = html.escape(server_name)
        lines = [
            f"ℹ️ <b>Расхождения БД ↔ панель — сервер {safe_server}</b> (режим report)",
            "",
            f"📉 Пропали с панели: {len(missing)}",
            f"📈 Лишнее на панели: {len(extra)}",
            "",
            "Действия не выполнены. Разберите вручную или включите режим ask.",
        ]
        text = "\n".join(lines)

        bot = await self._get_bot()
        for admin_id in admin_ids:
            try:
                await bot.send_message(chat_id=admin_id, text=text, parse_mode="HTML")
            except Exception as e:
                logger.warning("Не удалось отправить отчёт о расхождениях админу {}: {}", admin_id, e)

    async def refresh_divergence_digest(self, batch_id: str, session) -> None:
        """Перестроить и отредактировать дайджест батча по мере решений."""
        from sqlalchemy import select

        from app.bot.keyboards.inline import get_divergence_digest_keyboard
        from app.database.models import PendingDivergence, Server
        from app.services.divergence_service import STATUS_OPEN

        pendings = (
            await session.execute(
                select(PendingDivergence)
                .where(PendingDivergence.batch_id == batch_id)
                .order_by(PendingDivergence.id)
            )
        ).scalars().all()
        if not pendings:
            return

        refs = None
        for pd in pendings:
            if pd.notify_message_refs:
                refs = pd.notify_message_refs
                break
        if not refs:
            return

        open_count = sum(1 for p in pendings if p.status == STATUS_OPEN)
        server_obj = await session.get(Server, pendings[0].server_id)
        server_name = server_obj.name if server_obj else "—"
        # is_mass определить из текста невозможно — при обновлении флаг опускаем.
        text = self._build_divergence_digest_text(server_name, pendings, is_mass=False)
        keyboard = (
            get_divergence_digest_keyboard(batch_id, open_count=open_count)
            if open_count > 0
            else None
        )

        bot = await self._get_bot()
        for chat_id, message_id in refs:
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                )
            except Exception as e:
                logger.warning(
                    "Не удалось обновить дайджест расхождений (chat={}, msg={}): {}",
                    chat_id, message_id, e,
                )
