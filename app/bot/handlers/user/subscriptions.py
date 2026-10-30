"""User subscription management handlers."""

from collections import defaultdict

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from loguru import logger

from app.bot.keyboards import get_back_keyboard
from app.bot.keyboards.inline import get_cancel_keyboard, get_public_templates_keyboard
from app.bot.states.user import UserRequestSubscription
from app.database import async_session_factory
from app.services.new_subscription_service import NewSubscriptionService
from app.services.notification_service import NotificationService
from app.services.subscription_request_service import SubscriptionRequestService
from app.services.subscription_template_service import SubscriptionTemplateService
from app.utils.texts import t

router = Router()


@router.callback_query(F.data == "my_subscriptions")
@router.callback_query(F.data.startswith("my_subscriptions_p_"))
async def show_my_subscriptions(callback: CallbackQuery, client) -> None:
    """Show user's subscriptions."""
    if not client:
        await callback.answer(
            t("user.errors.client_not_found", "❌ Клиент не найден."), show_alert=True
        )
        return

    page = 0
    if callback.data.startswith("my_subscriptions_p_"):
        try:
            page = int(callback.data.split("_p_")[1])
        except ValueError:
            pass

    try:
        async with async_session_factory() as session:
            from app.services.new_subscription_service import NewSubscriptionService

            service = NewSubscriptionService(session)
            subscriptions = await service.get_client_subscriptions(client.id)

        logger.debug(
            "Подписки клиента {}: найдено {}",
            client.id,
            len(subscriptions) if subscriptions else 0,
        )

        if not subscriptions:
            await callback.message.edit_text(
                t(
                    "user.subs.empty",
                    "📝 У вас пока нет подписок.\n\nСвяжитесь с администратором для создания подписки.",
                ),
                reply_markup=get_back_keyboard("main_menu"),
            )
            return

        per_page = 5
        total_count = len(subscriptions)
        total_pages = max(1, -(-total_count // per_page))

        if page < 0:
            page = 0
        elif page >= total_pages:
            page = total_pages - 1

        start_idx = page * per_page
        end_idx = start_idx + per_page
        current_subs = subscriptions[start_idx:end_idx]

        if total_pages > 1:
            text = t(
                "user.subs.list_header_paginated",
                "📝 Ваши подписки ({count}) | Страница {current} из {total}:\n\n",
                count=total_count,
                current=page + 1,
                total=total_pages
            )
        else:
            text = t(
                "user.subs.list_header", "📝 Ваши подписки ({count}):\n\n", count=total_count
            )

        builder = InlineKeyboardBuilder()

        for sub in current_subs:
            # Use new subscription status property
            status = sub.subscription_status

            text += t(
                "user.subs.list_item",
                "{status} <b>{name}</b>\n   Активных подключений: {active}/{total}\n\n",
                status=status,
                name=sub.name,
                active=sub.active_connections_count,
                total=len(sub.inbound_connections),
            )

            # Add button for each subscription
            builder.row(InlineKeyboardButton(
                text=t("user.subs.btn_sub", "📝 {name}", name=sub.name),
                callback_data=f"user_sub_select_{sub.id}",
            ))

        if total_pages > 1:
            pagination_row = []
            if page > 0:
                pagination_row.append(
                    InlineKeyboardButton(
                        text=t(
                            "keyboards.pagination.prev",
                            "⬅️ Назад ({page}/{total_pages})",
                            page=page,
                            total_pages=total_pages
                        ),
                        callback_data=f"my_subscriptions_p_{page - 1}",
                    )
                )
            pagination_row.append(
                InlineKeyboardButton(
                    text=t(
                        "keyboards.pagination.current",
                        "📄 {current}/{total}",
                        current=page + 1,
                        total=total_pages
                    ),
                    callback_data="instruction_page_current",
                )
            )
            if page < total_pages - 1:
                pagination_row.append(
                    InlineKeyboardButton(
                        text=t(
                            "keyboards.pagination.next",
                            "Вперед ➡️ ({next_page}/{total_pages})",
                            next_page=page + 2,
                            total_pages=total_pages
                        ),
                        callback_data=f"my_subscriptions_p_{page + 1}",
                    )
                )
            builder.row(*pagination_row)

        builder.row(InlineKeyboardButton(
            text=t("user.subs.btn_urls", "🔗 Subscription URLs"), callback_data="all_sub_urls"
        ))
        builder.row(InlineKeyboardButton(
            text=t("user.subs.btn_status", "📊 Сроки и остатки"),
            callback_data="show_subscription_status",
        ))
        builder.row(InlineKeyboardButton(text=t("common.btn_back", "🔙 Назад"), callback_data="main_menu"))

        await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
    except Exception as e:
        logger.error("Ошибка в show_my_subscriptions: {}", e, exc_info=True)
        await callback.message.answer(t("user.subs.error", "❌ Ошибка при загрузке подписок."))
    finally:
        import contextlib

        with contextlib.suppress(Exception):
            await callback.answer()


@router.callback_query(F.data == "all_sub_urls")
async def show_all_subscription_urls(callback: CallbackQuery, client) -> None:
    """Show all subscription URLs for user."""
    if not client:
        await callback.answer(
            t("user.errors.client_not_found", "❌ Клиент не найден."), show_alert=True
        )
        return

    try:
        async with async_session_factory() as session:
            service = NewSubscriptionService(session)
            urls = await service.get_subscription_urls(client.id)

        if not urls:
            await callback.answer(
                t("user.subs.no_active", "❌ Нет активных подписок."), show_alert=True
            )
            return

        # Group URLs by subscription ID (unique subscription = subscription_id)
        grouped_subs = defaultdict(list)
        for url_info in urls:
            sub_id = url_info["subscription_id"]
            grouped_subs[sub_id].append(url_info)

        max_length = 4096
        text = t("user.subs.urls_header", "🔗 Subscription URLs:\n\n")

        # Show subscription info with URLs
        for _sub_id, url_list in grouped_subs.items():
            sub_name = url_list[0]["subscription_name"]

            section = t("user.subs.url_group", "<b>Подписка: {name}</b>\n", name=sub_name)

            # Group by URL (different inbounds on same server have same URL)
            # Show unique URLs
            url_map = {}  # url -> server_name
            for url_info in url_list:
                url = url_info["url"]
                server_name = url_info["server_name"]
                if url not in url_map:
                    url_map[url] = server_name

            # Add URLs as text (without code blocks)
            for url, _server_name in url_map.items():
                section += f"{url}\n"

            section += "\n"

            # Check if adding this section would exceed limit
            if len(text) + len(section) > max_length:
                section = t(
                    "user.subs.urls_hidden",
                    "\n... (остальные подписки скрыты из-за ограничений Telegram)",
                )
                text += section
                break

            text += section

        builder = InlineKeyboardBuilder()
        builder.button(
            text=t("user.subs.btn_copy_urls", "📋 Скопировать все URL"),
            callback_data="copy_all_urls",
        )
        builder.button(text=t("common.btn_back", "🔙 Назад"), callback_data="my_subscriptions")
        builder.adjust(1)

        await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
    except Exception as e:
        logger.error("Ошибка в show_all_subscription_urls: {}", e, exc_info=True)
        await callback.message.answer(t("user.subs.error", "❌ Ошибка при загрузке подписок."))
    finally:
        import contextlib

        with contextlib.suppress(Exception):
            await callback.answer()


@router.callback_query(F.data == "copy_all_json_urls")
async def copy_all_json_urls(callback: CallbackQuery, client) -> None:
    """Copy all JSON subscription URLs to clipboard-friendly format."""
    if not client:
        await callback.answer(
            t("user.errors.client_not_found", "❌ Клиент не найден."), show_alert=True
        )
        return

    try:
        async with async_session_factory() as session:
            service = NewSubscriptionService(session)
            urls = await service.get_subscription_json_urls(client.id)

        if not urls:
            await callback.answer(
                t("user.subs.no_active", "❌ Нет активных подписок."), show_alert=True
            )
            return

        # Group URLs by subscription ID (unique subscription = subscription_id)
        from collections import defaultdict

        grouped_subs = defaultdict(list)
        for url_info in urls:
            sub_id = url_info["subscription_id"]
            grouped_subs[sub_id].append(url_info)

        # Build text with all URLs
        max_length = 4096 - 20  # Reserve space for markdown formatting
        text = ""

        for _sub_id, url_list in grouped_subs.items():
            # Group unique URLs by server (same URL = same server)
            url_map = {}
            for url_info in url_list:
                url = url_info["url"]
                server_name = url_info["server_name"]
                if url not in url_map:
                    url_map[url] = server_name

            for url, _server_name in url_map.items():
                # Check if adding this URL would exceed limit
                if len(text) + len(url) + 1 > max_length:  # +1 for newline
                    break
                text += f"{url}\n"

        # Send as new message instead of callback answer for better copy support
        await callback.message.answer(f"```\n{text}\n```", parse_mode="MarkdownV2")
    except Exception as e:
        logger.error("Ошибка в copy_all_json_urls: {}", e, exc_info=True)
        await callback.message.answer(t("user.subs.error", "❌ Ошибка при загрузке подписок."))
    finally:
        import contextlib

        with contextlib.suppress(Exception):
            await callback.answer()


@router.callback_query(F.data.startswith("user_sub_select_"))
async def show_user_subscription_details(callback: CallbackQuery, client) -> None:
    """Show subscription details for user."""
    if not client:
        await callback.answer(
            t("user.errors.client_not_found", "❌ Клиент не найден."), show_alert=True
        )
        return

    subscription_id = int(callback.data.split("_")[-1])

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer()

    async with async_session_factory() as session:
        service = NewSubscriptionService(session)
        subscription = await service.get_subscription(subscription_id)

        if not subscription or subscription.client_id != client.id:
            await callback.answer(t("user.subs.not_found", "❌ Подписка не найдена."), show_alert=True)
            return

        status = subscription.subscription_status

        has_xui = any(
            conn.inbound.server.xui_panel is not None
            for conn in subscription.inbound_connections
            if conn.is_enabled
        )
        token_text = (
            t(
                "user.subs.details_token",
                "\nТокен: <code>{token}</code>",
                token=subscription.subscription_token,
            )
            if has_xui
            else ""
        )

        text = t(
            "user.subs.details_header",
            "📝 Подписка: <b>{name}</b>\n\nСтатус: {status}\nСоздана: {created}\nАктивных подключений: {active}/{total}{token_text}\n\n",
            name=subscription.name,
            status=status,
            created=subscription.created_at.strftime("%d.%m.%Y %H:%M"),
            active=subscription.active_connections_count,
            total=len(subscription.inbound_connections),
            token_text=token_text,
        )

        server_groups = defaultdict(list)
        builder = InlineKeyboardBuilder()

        for conn in subscription.inbound_connections:
            if conn.is_enabled:
                server = conn.inbound.server
                inbound = conn.inbound

                if inbound.type in ("awg_inbound",):
                    config_type = "file"
                    has_vpn_uri = bool(server.awg_service)
                    config_data = None
                elif inbound.type in ("mtproxy_inbound",):
                    config_type = "link"
                    if conn.secret:
                        port = server.mtproxy_service.port if server.mtproxy_service else 443
                        config_data = f"tg://proxy?server={server.ip_address}&port={port}&secret={conn.secret}"
                    else:
                        config_type = "empty"
                        config_data = None
                    has_vpn_uri = False
                else:
                    try:
                        from app.services.vpn_providers import get_vpn_provider

                        provider = get_vpn_provider(server, inbound_type=inbound.type)
                        config_dict = await provider.get_client_config(inbound, conn)
                        config_type = config_dict.get("config_type")
                        config_data = config_dict.get("config_data")
                        has_vpn_uri = bool(config_dict.get("vpn_uri"))
                        import contextlib

                        with contextlib.suppress(Exception):
                            await provider.close()
                    except Exception as e:
                        logger.warning("Не удалось получить конфиг для подключения {}: {}", conn.id, e)
                        config_type = "empty"
                        config_data = None
                        has_vpn_uri = False

                server_groups[server.id].append(
                    {
                        "server_name": server.name,
                        "inbound": inbound,
                        "connection": conn,
                        "config_type": config_type,
                        "config_data": config_data,
                        "vpn_uri": has_vpn_uri,
                    }
                )

        if server_groups:
            text += t("user.subs.active_connections", "📢 Активные подключения:\n\n")
            for _server_id, conn_list in server_groups.items():
                server_name = conn_list[0]["server_name"]
                text += f"🖥️ <b>{server_name}:</b>\n"

                url_groups = defaultdict(list)
                files = []
                empties = []

                for c in conn_list:
                    if c["config_type"] == "link":
                        url_groups[c["config_data"]].append(c)
                    elif c["config_type"] == "file":
                        files.append(c)
                    else:
                        empties.append(c)

                for url, url_items in url_groups.items():
                    remarks = []
                    for c in url_items:
                        rem = c["inbound"].remark.split(":")[0] if c["inbound"].remark else "Без названия"
                        if rem not in remarks:
                            remarks.append(rem)
                    combined_remark = ", ".join(remarks)

                    is_tg_proxy = url.startswith("tg://") or "t.me" in url
                    if is_tg_proxy:
                        text += f"  - {combined_remark}: {url}\n"
                    else:
                        text += f"  - {combined_remark}: <code>{url}</code>\n"
                        from aiogram.types import CopyTextButton
                        label = f"📋 Скопировать | {combined_remark}"
                        builder.row(InlineKeyboardButton(
                            text=label,
                            copy_text=CopyTextButton(text=url),
                        ))

                    conn = url_items[0]["connection"]
                    traffic = t("user.subs.unlimited", "Безлимит") if conn.is_unlimited else t("user.subs.traffic_gb", "{gb} GB", gb=conn.total_gb)
                    from app.utils.date_utils import format_expiry_date
                    expiry_info = format_expiry_date(conn.expiry_date, include_time=False)
                    text += t("user.subs.conn_traffic_indented", "    Трафик: {traffic}\n", traffic=traffic)
                    text += t("user.subs.conn_expiry_indented", "    Срок: {expiry}\n\n", expiry=expiry_info)

                for c in files:
                    conn = c["connection"]
                    inbound = c["inbound"]
                    remark = inbound.remark.split(":")[0] if inbound.remark else "Без названия"
                    text += f"  - {remark}\n"

                    traffic = t("user.subs.unlimited", "Безлимит") if conn.is_unlimited else t("user.subs.traffic_gb", "{gb} GB", gb=conn.total_gb)
                    from app.utils.date_utils import format_expiry_date
                    expiry_info = format_expiry_date(conn.expiry_date, include_time=False)
                    text += t("user.subs.conn_traffic_indented", "    Трафик: {traffic}\n", traffic=traffic)
                    text += t("user.subs.conn_expiry_indented", "    Срок: {expiry}\n\n", expiry=expiry_info)

                    has_vpn_uri = c.get("vpn_uri")
                    if has_vpn_uri:
                        uri_btn = InlineKeyboardButton(
                            text=f"🔗 {remark}",
                            callback_data=f"user_uri_conf_{conn.id}",
                        )
                        dl_btn = InlineKeyboardButton(
                            text=f"📥 {remark}", callback_data=f"user_dl_conf_{conn.id}"
                        )
                        builder.row(uri_btn, dl_btn)
                    else:
                        builder.row(InlineKeyboardButton(
                            text=f"📥 {remark}", callback_data=f"user_dl_conf_{conn.id}"
                        ))

                for _c in empties:
                    text += t("user.subs.conn_empty", "  - Конфиг недоступен (не установлен или ошибка)\n\n")

    builder.row(InlineKeyboardButton(
        text=t("user.subs.btn_back_to_subs", "🔙 Назад к подпискам"),
        callback_data="my_subscriptions",
    ))

    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")


@router.callback_query(F.data == "admin_export")
async def export_database(callback: CallbackQuery, client) -> None:
    """Export database to file and send to user (admin only)."""
    if not client or not client.is_admin:
        await callback.answer(
            t("user.errors.admin_only", "❌ Эта функция доступна только администраторам."),
            show_alert=True,
        )
        return

    try:
        # Get database file path from config
        import shutil
        from pathlib import Path

        from app.config import get_settings

        settings = get_settings()

        # Extract database path from connection URL
        db_path = Path(settings.database_url.replace("sqlite+aiosqlite:///", ""))

        # Check if database file exists
        if not db_path.exists():
            await callback.answer(
                t("admin.export.file_not_found", "❌ Файл базы данных не найден."), show_alert=True
            )
            return

        # Check file size (max 1.5 GB)
        file_size = db_path.stat().st_size
        if file_size > 1.5 * 1024 * 1024 * 1024:
            await callback.answer(
                t(
                    "admin.export.file_too_large",
                    "❌ Файл базы данных слишком большой ({size:.1f} MB). Максимум: 1.5 GB",
                    size=file_size / (1024 * 1024),
                ),
                show_alert=True,
            )
            return

        # Create temporary copy to avoid file locking issues
        import tempfile

        temp_dir = tempfile.gettempdir()
        temp_db_path = Path(temp_dir) / f"vpn_manager_export_{client.id}.db"

        try:
            # Show preparation message
            await callback.answer(t("admin.export.preparing", "⏳ Подготовка файла базы данных..."))

            # Copy database file to avoid locking issues
            shutil.copy2(db_path, temp_db_path)
            logger.info("База данных скопирована для экспорта: {}", temp_db_path)

            # Send database file
            from datetime import datetime

            from aiogram.types import FSInputFile

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

            document = FSInputFile(path=temp_db_path, filename=f"vpn_manager_{timestamp}.db")
            await callback.message.answer_document(
                document=document,
                caption=t(
                    "admin.export.caption",
                    "📄 Экспорт базы данных VPN Manager\nРазмер: {size:.2f} MB\nДата: {date}",
                    size=file_size / (1024 * 1024),
                    date=datetime.now().strftime("%d.%m.%Y %H:%M"),
                ),
            )

            await callback.answer(t("admin.export.success", "✅ База данных отправлена!"))
            logger.info("База данных экспортирована администратором {}: {}", client.id, db_path)

        finally:
            # Clean up temporary file
            if temp_db_path.exists():
                try:
                    temp_db_path.unlink()
                    logger.debug("Временный файл БД удалён: {}", temp_db_path)
                except Exception as e:
                    logger.warning("Не удалось удалить временный файл {}: {}", temp_db_path, e)

    except Exception as e:
        logger.error("Ошибка экспорта базы данных для администратора {}: {}", client.id, e, exc_info=True)
        await callback.answer(
            t("admin.export.error", "❌ Ошибка при экспорте базы данных."), show_alert=True
        )


@router.callback_query(F.data == "show_subscription_status")
async def show_subscription_status(callback: CallbackQuery, client) -> None:
    """Show subscription status including expiry and traffic information."""
    if not client:
        await callback.answer(
            t("user.errors.client_not_found", "❌ Клиент не найден."), show_alert=True
        )
        return

    async with async_session_factory() as session:
        from app.services.new_subscription_service import NewSubscriptionService

        service = NewSubscriptionService(session)
        subscriptions = await service.get_client_subscriptions(client.id)

    if not subscriptions:
        await callback.answer(
            t("user.subs.no_active", "❌ Нет активных подписок."), show_alert=True
        )
        return

    text = t("user.status.header", "📊 <b>Сроки и остатки подписок</b>\n\n")

    # Get XUI service for traffic data
    from app.services import XUIService

    xui_service = None
    xui_clients = {}

    for sub in subscriptions:
        # Get enabled connections (data is already eager loaded)
        enabled_connections = [conn for conn in sub.inbound_connections if conn.is_enabled]

        if not enabled_connections:
            text += t("user.status.sub_header_empty", "❌ <b>{name}</b>\n", name=sub.name)
            text += t("user.status.no_connections", "   Нет активных подключений\n\n")
            continue

        # Subscription-level expiry (from subscription or from connections)
        from app.utils.date_utils import format_expiry_date

        expiry_text = format_expiry_date(sub.expiry_date, include_time=True)

        text += t("user.status.sub_header", "📦 <b>{name}</b>\n", name=sub.name)
        text += t("user.status.sub_expiry", "   📅 Срок: {expiry}\n", expiry=expiry_text)
        text += t(
            "user.status.sub_connections",
            "   🔌 Активных подключений: {active}/{total}\n\n",
            active=sub.active_connections_count,
            total=len(enabled_connections),
        )

        # Show per-connection details
        text += t("user.status.connections_header", "   <b>Подключения:</b>\n")

        for conn in enabled_connections:
            inbound = conn.inbound
            server = inbound.server

            # Connection expiry
            conn_expiry = format_expiry_date(conn.expiry_date, include_time=True)

            # Connection traffic
            if conn.is_unlimited:
                traffic_text = t("user.subs.unlimited", "Безлимит")
            else:
                traffic_text = t("user.status.traffic_gb", "{gb} ГБ", gb=conn.total_gb)

                # Get actual traffic from XUI
                if conn.type == "xui_inbound_connection":
                    try:
                        async with async_session_factory() as traffic_session:
                            xui_service = XUIService(traffic_session)
                            if server.id not in xui_clients:
                                xui_clients[server.id] = await xui_service._get_client(server)

                            xui_client = xui_clients[server.id]
                            all_clients = await xui_client.get_clients()
                            clients = [
                                c for c in all_clients
                                if inbound.xui_id in c.get("inboundIds", [])
                            ]

                            for xui_conn in clients:
                                if xui_conn.get("uuid") == conn.uuid or xui_conn.get("id") == conn.uuid:
                                    used_gb = (xui_conn.get("up", 0) + xui_conn.get("down", 0)) / (
                                        1024**3
                                    )
                                    remaining_gb = conn.total_gb - used_gb

                                    if remaining_gb <= 5:
                                        traffic_text += t(
                                            "user.status.traffic_low",
                                            " (⚠️ осталось {gb:.2f} ГБ)",
                                            gb=remaining_gb,
                                        )
                                    else:
                                        traffic_text += t(
                                            "user.status.traffic_remaining",
                                            " (осталось {gb:.2f} ГБ)",
                                            gb=remaining_gb,
                                        )
                                    break
                    except Exception as e:
                        logger.warning("Не удалось получить трафик для подключения {}: {}", conn.id, e)
                        traffic_text += t("user.status.traffic_error", " (ошибка получения данных)")

            # Add connection status indicator
            conn_status = "✅" if conn.is_connection_active else "❌"
            display_remark = inbound.remark.split(":")[0]
            text += t(
                "user.status.conn_info",
                "      {status} {remark} | {server}\n",
                status=conn_status,
                remark=display_remark,
                server=server.name,
            )
            text += t("user.status.conn_expiry", "        📅 Срок: {expiry}\n", expiry=conn_expiry)
            text += t(
                "user.status.conn_traffic", "        📊 Трафик: {traffic}\n", traffic=traffic_text
            )

        text += "\n"

    builder = InlineKeyboardBuilder()
    builder.button(
        text=t("user.subs.btn_back_to_subs", "🔙 Назад к подпискам"),
        callback_data="my_subscriptions",
    )
    builder.adjust(1)

    try:
        await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
        await callback.answer()
    finally:
        # Close XUI clients
        for client_obj in xui_clients.values():
            try:
                await client_obj.close()
            except Exception as e:
                logger.warning("Ошибка при закрытии XUI-клиента: {}", e)


@router.callback_query(F.data == "request_subscription")
async def start_subscription_request(callback: CallbackQuery, client) -> None:
    """Start subscription request process for user."""
    if not client:
        await callback.answer(
            t("user.errors.client_not_found", "❌ Клиент не найден."), show_alert=True
        )
        return

    async with async_session_factory() as session:
        req_service = SubscriptionRequestService(session)
        pending_count = await req_service.get_pending_requests_count(client.id)
        if pending_count >= 5:
            await callback.answer(
                t("user.req.too_many", "У вас слишком много ожидающих заявок."), show_alert=True
            )
            return

        tpl_service = SubscriptionTemplateService(session)
        public_templates = await tpl_service.get_public_templates()

    if not public_templates:
        await callback.answer(
            t("user.req.unavailable", "В данный момент запросы недоступны."), show_alert=True
        )
        return

    text = t("user.req.choose_template", "Выберите шаблон для новой подписки:\n\n")
    for tpl in public_templates:
        desc = tpl.description or t("user.req.no_description", "Нет описания")
        text += t(
            "user.req.template_item", "📦 <b>{name}</b>\n   {desc}\n\n", name=tpl.name, desc=desc
        )

    keyboard = get_public_templates_keyboard(public_templates)
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("user_req_tpl_"))
async def handle_template_selection(callback: CallbackQuery, state: FSMContext, client) -> None:
    """Handle template selection for subscription request."""
    if not client:
        await callback.answer(
            t("user.errors.client_not_found", "❌ Клиент не найден."), show_alert=True
        )
        return

    template_id = int(callback.data.split("_")[-1])
    await state.update_data(template_id=template_id)
    await state.set_state(UserRequestSubscription.waiting_for_name)

    await callback.message.answer(
        t(
            "user.req.enter_name",
            "Введите понятное название для вашей подписки (например: Мой Телефон):",
        ),
        reply_markup=get_cancel_keyboard(),
    )
    await callback.answer()


@router.message(UserRequestSubscription.waiting_for_name)
async def process_subscription_request_name(message: Message, state: FSMContext, client) -> None:
    """Process entered name and create subscription request."""
    if not client:
        return

    if not message.text:
        return

    requested_name = message.text.strip()
    if not requested_name:
        await message.answer(t("user.req.invalid_name", "Пожалуйста, введите корректное название."))
        return

    data = await state.get_data()
    template_id = data.get("template_id")

    async with async_session_factory() as session:
        req_service = SubscriptionRequestService(session)
        request = await req_service.create_request(
            client_id=client.id,
            template_id=template_id,
            requested_name=requested_name,
        )

        tpl_service = SubscriptionTemplateService(session)
        template = await tpl_service.get_template(template_id)

        await session.commit()

        notification_service = NotificationService(session)
        await notification_service.notify_admins_new_request(request, template.name)

    await message.answer(
        t(
            "user.req.success",
            "✅ Ваш запрос отправлен администратору. В ближайшее время вы получите ответ",
        )
    )
    await state.clear()


@router.callback_query(F.data.startswith("user_uri_conf_"))
async def send_vpn_uri(callback: CallbackQuery, client) -> None:
    if not client:
        await callback.answer(
            t("user.errors.client_not_found", "❌ Клиент не найден."), show_alert=True
        )
        return

    conn_id = int(callback.data.split("_")[-1])

    async with async_session_factory() as session:
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        from app.database.models import Inbound, InboundConnection
        from app.database.models.server import Server

        conn_result = await session.execute(
            select(InboundConnection)
            .where(InboundConnection.id == conn_id)
            .options(
                selectinload(InboundConnection.inbound)
                .selectinload(Inbound.server)
                .selectinload(Server.awg_service),
                selectinload(InboundConnection.subscription),
            )
        )
        conn = conn_result.scalar_one_or_none()

        if not conn or conn.subscription.client_id != client.id:
            await callback.answer(
                t("user.subs.not_found", "❌ Подключение не найдено."), show_alert=True
            )
            return

        from app.services.vpn_providers import get_vpn_provider

        provider = get_vpn_provider(conn.inbound.server, inbound_type=conn.inbound.type)

        await callback.answer()

        try:
            config = await provider.get_client_config(conn.inbound, conn)
            vpn_uri = config.get("vpn_uri")

            if not vpn_uri:
                await callback.message.answer("❌ Ссылка недоступна для этого подключения.")
                return

            await callback.message.answer(
                f"<code>{vpn_uri}</code>",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error("Не удалось получить vpn_uri для подключения {}: {}", conn_id, e, exc_info=True)
            await callback.message.answer(
                t("user.subs.download_error", "❌ Ошибка при получении ссылки.")
            )
        finally:
            import contextlib

            with contextlib.suppress(Exception):
                await provider.close()

            builder = InlineKeyboardBuilder()
            builder.button(
                text=t("user.subs.btn_back_to_sub", "🔙 Назад к подписке"),
                callback_data=f"user_sub_select_{conn.subscription.id}",
            )
            builder.button(
                text=t("user.subs.btn_back_to_subs", "🔙 Назад к подпискам"),
                callback_data="my_subscriptions",
            )
            builder.adjust(1)
            import contextlib

            with contextlib.suppress(Exception):
                await callback.message.edit_reply_markup(reply_markup=builder.as_markup())


@router.callback_query(F.data.startswith("user_dl_conf_"))
async def download_file_config(callback: CallbackQuery, client) -> None:
    """Download file config (like Wireguard .conf) and QR code."""
    if not client:
        await callback.answer(
            t("user.errors.client_not_found", "❌ Клиент не найден."), show_alert=True
        )
        return

    conn_id = int(callback.data.split("_")[-1])

    async with async_session_factory() as session:
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        from app.database.models import Inbound, InboundConnection
        from app.database.models.server import Server

        conn_result = await session.execute(
            select(InboundConnection)
            .where(InboundConnection.id == conn_id)
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
                selectinload(InboundConnection.subscription),
            )
        )
        conn = conn_result.scalar_one_or_none()

        if not conn or conn.subscription.client_id != client.id:
            await callback.answer(
                t("user.subs.not_found", "❌ Подключение не найдено."), show_alert=True
            )
            return

        from app.services.vpn_providers import get_vpn_provider

        provider = get_vpn_provider(conn.inbound.server, inbound_type=conn.inbound.type)

        await callback.answer(t("user.subs.downloading", "⏳ Загрузка конфига..."))

        try:
            config = await provider.get_client_config(conn.inbound, conn)

            if config.get("error") == "wipe_detected":
                await callback.message.answer(
                    t(
                        "user.subs.wipe_detected",
                        "⚠️ Конфиг временно недоступен: сервер AmneziaWG переустанавливается. "
                        "Обратитесь к администратору.",
                    )
                )
                return

            if config.get("config_data"):
                from aiogram.types import BufferedInputFile

                config_data = config["config_data"]
                config_type = config.get("config_type", "file")

                if config_type == "file":
                    file_content = config_data.encode("utf-8")
                    display_remark = conn.inbound.remark.split(":")[0]
                    filename = f"{conn.subscription.name}_{display_remark}.conf".replace(
                        " ", "_"
                    )
                    doc = BufferedInputFile(file_content, filename=filename)
                    await callback.message.answer_document(
                        document=doc,
                        caption=t(
                            "user.subs.config_caption",
                            "📁 Ваш конфигурационный файл для {remark}",
                            remark=display_remark,
                        ),
                    )

                    # QR нативного .conf — так же экспортирует официальный клиент
                    # Amnezia (Share → конфиг): телефон сканирует и подключается.
                    try:
                        import io

                        import qrcode

                        qr = qrcode.QRCode(version=1, box_size=10, border=4)
                        qr.add_data(config_data)
                        qr.make(fit=True)
                        img = qr.make_image(fill_color="black", back_color="white")

                        bio = io.BytesIO()
                        img.save(bio, "PNG")
                        bio.seek(0)

                        photo = BufferedInputFile(bio.getvalue(), filename="qr.png")
                        await callback.message.answer_photo(
                            photo=photo,
                            caption=t(
                                "user.subs.qr_caption",
                                "📱 QR-код для {remark}",
                                remark=display_remark,
                            ),
                        )
                    except Exception as qr_err:
                        logger.warning("Не удалось сгенерировать QR: {}", qr_err)

                else:
                    # Send as link message
                    if config_data.startswith("tg://") or "t.me" in config_data:
                        await callback.message.answer(
                            text=t(
                                "user.subs.link_caption_clickable",
                                "🔗 Ссылка для {remark}:\n{link}",
                                remark=conn.inbound.remark,
                                link=config_data,
                            ),
                            parse_mode="HTML",
                        )
                    else:
                        await callback.message.answer(
                            text=t(
                                "user.subs.link_caption",
                                "🔗 Ссылка для {remark}:\n<code>{link}</code>",
                                remark=conn.inbound.remark,
                                link=config_data,
                            ),
                            parse_mode="HTML",
                        )

        except Exception as e:
            from loguru import logger

            logger.error("Ошибка при скачивании конфига для подключения {}: {}", conn_id, e, exc_info=True)
            await callback.message.answer(
                t("user.subs.download_error", "❌ Ошибка при скачивании конфига.")
            )

        finally:
            try:
                await provider.close()
            except Exception as e:
                from loguru import logger

                logger.warning("Не удалось закрыть провайдер: {}", e)

            builder = InlineKeyboardBuilder()
            builder.button(
                text=t("user.subs.btn_back_to_sub", "🔙 Назад к подписке"),
                callback_data=f"user_sub_select_{conn.subscription.id}",
            )
            builder.button(
                text=t("user.subs.btn_back_to_subs", "🔙 Назад к подпискам"),
                callback_data="my_subscriptions",
            )
            builder.adjust(1)
            try:
                await callback.message.edit_reply_markup(reply_markup=builder.as_markup())
            except Exception as e:
                logger.warning("Не удалось восстановить клавиатуру: {}", e)
