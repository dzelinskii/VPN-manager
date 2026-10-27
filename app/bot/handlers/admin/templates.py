"""Template management handlers for subscription templates."""

import asyncio
import html

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from loguru import logger

from app.bot.filters import AdminFilter
from app.bot.keyboards import (
    get_back_keyboard,
    get_confirm_keyboard,
    get_inbound_selection_for_template,
    get_servers_keyboard_for_template_edit,
    get_template_actions_keyboard,
    get_template_edit_inbounds_keyboard,
    get_template_edit_menu_keyboard,
    get_template_inbounds_keyboard,
    get_template_multi_select_confirm_keyboard,
    get_template_multi_select_keyboard,
    get_templates_keyboard,
)
from app.bot.states.admin import TemplateManagement
from app.database import async_session_factory
from app.services.client_service import ClientService
from app.services.pricing import format_price, parse_price_kopecks, set_template_price
from app.services.subscription_template_service import SubscriptionTemplateService
from app.utils.texts import t
from app.xui_client.exceptions import XUIError

router = Router()
router.message.filter(AdminFilter())
router.callback_query.filter(AdminFilter())


@router.callback_query(F.data == "admin_templates")
async def show_templates(callback: CallbackQuery, state: FSMContext):
    """Show templates list."""
    current_state = await state.get_state()
    if current_state:
        await state.clear()

    async with async_session_factory() as session:
        template_service = SubscriptionTemplateService(session)
        templates = await template_service.get_all_templates()

        if not templates:
            text = t(
                "admin.templates.no_templates",
                "📋 <b>Шаблоны подписок</b>\n\nШаблонов пока нет. Создайте первый шаблон!",
            )
            # Create keyboard with create template button
            from aiogram.utils.keyboard import InlineKeyboardBuilder

            builder = InlineKeyboardBuilder()
            builder.button(
                text=t("admin.templates.btn_create_template", "➕ Создать шаблон"),
                callback_data="template_add",
            )
            builder.button(
                text=t("admin.templates.btn_back", "Назад"), callback_data="admin_clients_menu"
            )
            builder.adjust(1)
            keyboard = builder.as_markup()
        else:
            text = t(
                "admin.templates.list",
                "📋 <b>Шаблоны подписок</b>\n\nВсего шаблонов: {count}",
                count=len(templates),
            )
            keyboard = get_templates_keyboard(templates)

    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data == "template_add")
async def start_template_creation(callback: CallbackQuery, state: FSMContext):
    """Start template creation."""
    await state.clear()
    await state.set_state(TemplateManagement.waiting_for_template_name)

    text = t(
        "admin.templates.create_prompt_name",
        "📝 <b>Создание шаблона подписки</b>\n\nВведите название шаблона:",
    )

    await callback.message.edit_text(text, reply_markup=get_back_keyboard("admin_templates"))
    await callback.answer()


@router.message(TemplateManagement.waiting_for_template_name)
async def handle_template_name(message: Message, state: FSMContext):
    """Handle template name input."""
    name = message.text.strip()

    if not name:
        await message.answer(
            t("admin.templates.name_empty", "⚠️ Название не может быть пустым. Введите название:"),
            reply_markup=get_back_keyboard("admin_templates"),
        )
        return

    if len(name) > 100:
        await message.answer(
            t(
                "admin.templates.name_too_long",
                "⚠️ Название слишком длинное. Максимум 100 символов. Введите название:",
            ),
            reply_markup=get_back_keyboard("admin_templates"),
        )
        return

    await state.update_data(template_name=name)
    await state.set_state(TemplateManagement.waiting_for_template_description)

    text = t(
        "admin.templates.create_prompt_desc",
        "✅ <b>Название:</b> {name}\n\n"
        "Введите описание шаблона (необязательно, отправьте /skip для пропуска):",
        name=name,
    )

    await message.answer(text, reply_markup=get_back_keyboard("admin_templates"))


@router.message(TemplateManagement.waiting_for_template_description)
async def handle_template_description(message: Message, state: FSMContext):
    """Handle template description input."""
    if message.text == "/skip":
        description = None
    else:
        description = message.text.strip()
        if len(description) > 500:
            await message.answer(
                t(
                    "admin.templates.desc_too_long",
                    "⚠️ Описание слишком длинное. Максимум 500 символов. Введите описание:",
                )
            )
            return

    await state.update_data(template_description=description)
    await state.set_state(TemplateManagement.waiting_for_default_traffic)

    text = t(
        "admin.templates.create_prompt_traffic",
        "📝 <b>Описание:</b> {description}\n\n"
        "Введите лимит трафика по умолчанию в ГБ (0 = безлимитный):",
        description=description or t("admin.templates.no_description", "Нет описания"),
    )

    await message.answer(text)


@router.message(TemplateManagement.waiting_for_default_traffic)
async def handle_default_traffic(message: Message, state: FSMContext):
    """Handle default traffic limit input."""
    try:
        traffic = int(message.text.strip())
        if traffic < 0:
            await message.answer(
                t(
                    "admin.templates.traffic_negative",
                    "⚠️ Лимит трафика не может быть отрицательным. Введите число:",
                )
            )
            return
    except ValueError:
        await message.answer(
            t("admin.templates.traffic_invalid", "⚠️ Введите корректное число (0 = безлимитный):")
        )
        return

    await state.update_data(default_traffic=traffic)
    await state.set_state(TemplateManagement.waiting_for_default_expiry)

    traffic_text = t(
        "admin.templates.traffic_format",
        "{traffic} ГБ {unlimited}",
        traffic=traffic,
        unlimited=t("admin.templates.unlimited", "(безлимитный)") if traffic == 0 else "",
    ).strip()

    text = t(
        "admin.templates.create_prompt_expiry",
        "📊 <b>Лимит трафика:</b> {traffic_text}\n\n"
        "Введите срок действия по умолчанию в днях (0 = бессрочный, отправьте /skip для пропуска):",
        traffic_text=traffic_text,
    )

    await message.answer(text)


@router.message(TemplateManagement.waiting_for_default_expiry)
async def handle_default_expiry(message: Message, state: FSMContext):
    """Handle default expiry days input."""
    if message.text == "/skip":
        expiry = None
    else:
        try:
            expiry = int(message.text.strip())
            if expiry < 0:
                await message.answer(
                    t(
                        "admin.templates.expiry_negative",
                        "⚠️ Срок действия не может быть отрицательным. Введите число:",
                    )
                )
                return
        except ValueError:
            await message.answer(
                t(
                    "admin.templates.expiry_invalid",
                    "⚠️ Введите корректное число (0 = бессрочный, /skip = использовать шаблон):",
                )
            )
            return

        if expiry == 0:
            expiry = None

    await state.update_data(default_expiry=expiry)
    await state.set_state(TemplateManagement.waiting_for_template_notes)

    data = await state.get_data()
    data.get("default_traffic")
    expiry_text = (
        t("admin.templates.days_format", "{count} дн.", count=expiry)
        if expiry
        else t("admin.templates.unlimited_time_capital", "Бессрочный")
    )

    text = t(
        "admin.templates.create_prompt_notes",
        "📅 <b>Срок действия:</b> {expiry_text}\n\n"
        "Введите заметки (необязательно, отправьте /skip для пропуска):",
        expiry_text=expiry_text,
    )

    await message.answer(text)


@router.message(TemplateManagement.waiting_for_template_notes)
async def handle_template_notes(message: Message, state: FSMContext):
    """Handle template notes input."""
    if message.text == "/skip":
        notes = None
    else:
        notes = message.text.strip()
        if len(notes) > 500:
            await message.answer(
                t(
                    "admin.templates.notes_too_long",
                    "⚠️ Заметки слишком длинные. Максимум 500 символов. Введите заметки:",
                )
            )
            return

    data = await state.get_data()

    try:
        async with async_session_factory() as session:
            template_service = SubscriptionTemplateService(session)
            template = await template_service.create_template(
                name=data["template_name"],
                description=data["template_description"],
                default_total_gb=data["default_traffic"],
                default_expiry_days=data["default_expiry"],
                notes=notes,
            )
            await session.commit()

            logger.info("Шаблон {} создан (ID: {})", template.name, template.id)

        await state.clear()

        traffic_limit = (
            f"{template.default_total_gb} ГБ"
            if template.default_total_gb > 0
            else t("admin.templates.unlimited_capital", "Безлимитный")
        )
        expiry_text = (
            t("admin.templates.days_format", "{count} дн.", count=template.default_expiry_days)
            if template.default_expiry_days
            else t("admin.templates.unlimited_time_capital", "Бессрочный")
        )

        text = t(
            "admin.templates.created_success",
            "✅ <b>Шаблон создан!</b>\n\n"
            "📋 <b>{name}</b>\n"
            "📝 {description}\n"
            "📊 <b>Лимит трафика:</b> {traffic}\n"
            "📅 <b>Срок действия:</b> {expiry}\n",
            name=template.name,
            description=template.description or t("admin.templates.no_description", "Нет описания"),
            traffic=traffic_limit,
            expiry=expiry_text,
        )

        if template.notes:
            text += t(
                "admin.templates.notes_field", "\n📌 <b>Заметки:</b> {notes}", notes=template.notes
            )

        keyboard = get_back_keyboard("admin_templates")

        await message.answer(text, reply_markup=keyboard)

    except XUIError as e:
        await state.clear()
        logger.warning("Создание шаблона не удалось: {}", e)
        await message.answer(t("admin.templates.error_prefix", "❌ {error}", error=str(e)))
    except Exception as e:
        await state.clear()
        logger.error("Ошибка создания шаблона: {}", e, exc_info=True)
        await message.answer(
            t(
                "admin.templates.create_error",
                "❌ Произошла ошибка при создании шаблона: {error}",
                error=str(e),
            )
        )


@router.callback_query(
    F.data.startswith("template_select_") & ~F.data.startswith("template_select_inbound_")
)
async def show_template_details(callback: CallbackQuery):
    """Show template details."""
    template_id = int(callback.data.split("_")[2])

    async with async_session_factory() as session:
        template_service = SubscriptionTemplateService(session)
        template = await template_service.get_template(template_id)

        if not template:
            await callback.answer(
                t("admin.templates.not_found", "⚠️ Шаблон не найден"), show_alert=True
            )
            return

        traffic_limit = (
            f"{template.default_total_gb} ГБ"
            if template.default_total_gb > 0
            else t("admin.templates.unlimited_capital", "Безлимитный")
        )
        expiry_text = (
            t("admin.templates.days_format", "{count} дн.", count=template.default_expiry_days)
            if template.default_expiry_days
            else t("admin.templates.unlimited_time_capital", "Бессрочный")
        )
        inbounds_count = len(template.template_inbounds)

        text = t(
            "admin.templates.details",
            "📋 <b>{name}</b>\n\n"
            "📝 {description}\n"
            "📊 <b>Лимит трафика:</b> {traffic}\n"
            "📅 <b>Срок действия:</b> {expiry}\n"
            "💰 <b>Цена:</b> {price}\n"
            "🔌 <b>Подключений:</b> {count}\n",
            name=template.name,
            description=template.description or t("admin.templates.no_description", "Нет описания"),
            traffic=traffic_limit,
            expiry=expiry_text,
            price=format_price(template.price_kopecks),
            count=inbounds_count,
        )

        if template.notes:
            text += t(
                "admin.templates.notes_field", "\n📌 <b>Заметки:</b> {notes}", notes=template.notes
            )

        keyboard = get_template_actions_keyboard(template_id, template.is_public)

        await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("admin_tpl_toggle_public_"))
async def toggle_template_public_status(callback: CallbackQuery):
    """Toggle template public status."""
    template_id = int(callback.data.split("_")[4])

    async with async_session_factory() as session:
        template_service = SubscriptionTemplateService(session)
        template = await template_service.get_template(template_id)

        if not template:
            await callback.answer(
                t("admin.templates.not_found", "⚠️ Шаблон не найден"), show_alert=True
            )
            return

        updated_template = await template_service.update_template(
            template_id=template_id, is_public=not template.is_public
        )
        await session.commit()

        traffic_limit = (
            f"{updated_template.default_total_gb} ГБ"
            if updated_template.default_total_gb > 0
            else "Безлимитный"
        )
        expiry_text = (
            f"{updated_template.default_expiry_days} дн."
            if updated_template.default_expiry_days
            else "Бессрочный"
        )
        inbounds_count = len(updated_template.template_inbounds)

        text = (
            f"📋 <b>{updated_template.name}</b>\n\n"
            f"📝 {updated_template.description or 'Нет описания'}\n"
            f"📊 <b>Лимит трафика:</b> {traffic_limit}\n"
            f"📅 <b>Срок действия:</b> {expiry_text}\n"
            f"🔌 <b>Подключений:</b> {inbounds_count}\n"
        )

        if updated_template.notes:
            text += f"\n📌 <b>Заметки:</b> {updated_template.notes}"

        keyboard = get_template_actions_keyboard(template_id, updated_template.is_public)

        await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer(t("admin.templates.status_updated", "Статус обновлен"))


@router.callback_query(F.data.startswith("template_create_subscription_"))
async def start_subscription_from_template(
    callback: CallbackQuery, state: FSMContext
):
    """Start subscription creation from template."""
    template_id = int(callback.data.split("_")[3])

    await state.update_data(template_id=template_id)
    await state.set_state(TemplateManagement.waiting_for_client_selection)

    # Get template info
    async with async_session_factory() as session:
        template_service = SubscriptionTemplateService(session)
        template = await template_service.get_template(template_id)
        template_info = f"<b>{template.name}</b> ({len(template.template_inbounds)} подключений)"

    text = t(
        "admin.templates.create_sub_from_template",
        "📦 <b>Создание подписки из шаблона</b>\n\n"
        "Шаблон: {template_info}\n\n"
        "Выберите способ поиска клиента:",
        template_info=template_info,
    )

    from aiogram.utils.keyboard import InlineKeyboardBuilder

    builder = InlineKeyboardBuilder()
    builder.button(
        text=t("admin.templates.btn_search_client", "🔍 Поиск клиента"),
        callback_data=f"template_client_search_{template_id}",
    )
    builder.button(
        text=t("admin.templates.btn_all_clients", "👤 Все клиенты"),
        callback_data="template_show_all_clients",
    )
    builder.button(
        text=t("admin.templates.btn_back", "Назад"), callback_data=f"template_select_{template_id}"
    )
    builder.adjust(2)

    await callback.message.edit_text(text, reply_markup=builder.as_markup())
    await callback.answer()


@router.callback_query(F.data == "template_show_all_clients")
async def show_all_clients_for_template(callback: CallbackQuery, state: FSMContext):
    """Show all clients for template subscription creation."""
    data = await state.get_data()
    template_id = data.get("template_id")

    async with async_session_factory() as session:
        template_service = SubscriptionTemplateService(session)
        template = await template_service.get_template(template_id)
        template_info = f"<b>{template.name}</b> ({len(template.template_inbounds)} подключений)"

        client_service = ClientService(session)
        clients = await client_service.get_active_clients()

    if not clients:
        await callback.answer(
            t(
                "admin.templates.no_active_clients",
                "⚠️ Нет активных клиентов. Сначала создайте клиента.",
            ),
            show_alert=True,
        )
        return

    await state.set_state(TemplateManagement.waiting_for_client_selection)

    text = t(
        "admin.templates.create_sub_all_clients",
        "📦 <b>Создание подписки из шаблона</b>\n\n"
        "Шаблон: {template_info}\n\n"
        "Всего клиентов: {count}\n\n"
        "Выберите клиента:",
        template_info=template_info,
        count=len(clients),
    )

    from aiogram.utils.keyboard import InlineKeyboardBuilder

    builder = InlineKeyboardBuilder()
    for client in clients[:10]:  # Limit to 10 clients
        builder.button(
            text=f"👤 {client.name}", callback_data=f"template_client_select_{client.id}"
        )
    builder.button(
        text=t("admin.templates.btn_search_client", "🔍 Поиск клиента"),
        callback_data=f"template_client_search_{template_id}",
    )
    builder.button(
        text=t("admin.templates.btn_back", "Назад"), callback_data=f"template_select_{template_id}"
    )
    builder.adjust(1)

    await callback.message.edit_text(text, reply_markup=builder.as_markup())
    await callback.answer()


@router.callback_query(
    TemplateManagement.waiting_for_client_selection, F.data.startswith("template_client_select_")
)
async def handle_template_client_selection(callback: CallbackQuery, state: FSMContext):
    """Handle client selection for template."""
    client_id = int(callback.data.split("_")[3])
    await state.update_data(client_id=client_id)
    await state.set_state(TemplateManagement.waiting_for_subscription_name)

    async with async_session_factory() as session:
        client_service = ClientService(session)
        template_service = SubscriptionTemplateService(session)
        client = await client_service.get_client_by_id(client_id)
        template = await template_service.get_template(state.get_data()["template_id"])

    text = t(
        "admin.templates.sub_settings_prompt",
        "👤 <b>Клиент:</b> {client_name}\n"
        "📋 <b>Шаблон:</b> {template_name}\n\n"
        "⚙️ <b>Настройки подписки:</b>\n"
        "📊 Лимит трафика: {traffic}\n"
        "📅 Срок действия: {expiry}\n\n"
        "Введите название подписки:",
        client_name=client.name,
        template_name=template.name,
        traffic=f"{template.default_total_gb} ГБ"
        if template.default_total_gb > 0
        else t("admin.templates.unlimited_capital", "Безлимитный"),
        expiry=t("admin.templates.days_format", "{count} дн.", count=template.default_expiry_days)
        if template.default_expiry_days
        else t("admin.templates.unlimited_time_capital", "Бессрочный"),
    )

    await callback.message.edit_text(text)
    await callback.answer()


@router.message(TemplateManagement.waiting_for_subscription_name)
async def handle_template_subscription_name(message: Message, state: FSMContext):
    """Handle subscription name input and create subscription from template."""
    name = message.text.strip()

    if not name:
        await message.answer(
            t("admin.templates.name_empty", "⚠️ Название не может быть пустым. Введите название:")
        )
        return

    if len(name) > 100:
        await message.answer(
            t(
                "admin.templates.name_too_long",
                "⚠️ Название слишком длинное. Максимум 100 символов. Введите название:",
            )
        )
        return

    await state.update_data(subscription_name=name)

    data = await state.get_data()

    try:
        async with async_session_factory() as session:
            template_service = SubscriptionTemplateService(session)
            client_service = ClientService(session)

            # Get template data
            template = await template_service.get_template(data["template_id"])

            # Create subscription from template with template settings only
            subscription, _ = await template_service.create_subscription_from_template(
                template_id=data["template_id"],
                client_id=data["client_id"],
                subscription_name=name,
                total_gb=template.default_total_gb,  # Use template traffic limit
                expiry_days=template.default_expiry_days,  # Use template expiry
                notes=template.notes,  # Use template notes
            )

            # Reload subscription to eager-load relationships (connections, inbounds, servers)
            from app.services.new_subscription_service import NewSubscriptionService
            sub_service = NewSubscriptionService(session)
            subscription = await sub_service.get_subscription_by_id(subscription.id)

            connections = list(subscription.inbound_connections)

            # Get client for notification
            # Ensure client is fully loaded before detached usage
            client = await client_service.get_client_by_id(data["client_id"])

            # Send notification if client has telegram_id
            if client.telegram_id:
                from app.services.notification_service import NotificationService

                notification_service = NotificationService(session)
                await notification_service.notify_subscription_created(
                    client=client,
                    subscription=subscription,
                    connections=connections,
                )

            # Extract properties needed for message before committing (which expires objects)
            sub_total_gb = subscription.total_gb
            sub_expiry_date = subscription.expiry_date
            sub_remaining_days = subscription.remaining_days
            sub_name = subscription.name
            client_name = client.name
            client_has_tg = bool(client.telegram_id)
            conn_count = len(connections)

            await session.commit()

        await state.clear()

        traffic_text = (
            f"{sub_total_gb} ГБ"
            if sub_total_gb > 0
            else t("admin.templates.unlimited_capital", "Безлимитный")
        )
        expiry_text = (
            t("admin.templates.days_format", "{count} дн.", count=sub_remaining_days)
            if sub_expiry_date
            else t("admin.templates.unlimited_time_capital", "Бессрочный")
        )

        text = t(
            "admin.templates.sub_created_success",
            "✅ <b>Подписка создана!</b>\n\n"
            "👤 <b>Клиент:</b> {client_name}\n"
            "📦 <b>Подписка:</b> {sub_name}\n"
            "📊 <b>Лимит трафика:</b> {traffic}\n"
            "📅 <b>Срок действия:</b> {expiry}\n"
            "🔌 <b>Создано подключений:</b> {conn_count}\n",
            client_name=client_name,
            sub_name=sub_name,
            traffic=traffic_text,
            expiry=expiry_text,
            conn_count=conn_count,
        )

        if client_has_tg:
            text += t("admin.templates.notification_sent", "\n📱 <b>Уведомление отправлено:</b> Да")

        keyboard = get_back_keyboard("admin_clients_menu")

        await message.answer(text, reply_markup=keyboard)

    except XUIError as e:
        await state.clear()
        await message.answer(
            t(
                "admin.templates.create_sub_error_xui",
                "❌ Ошибка при создании подписки: {error}",
                error=str(e),
            )
        )
        return
    except Exception as e:
        await state.clear()
        logger.error("Ошибка создания подписки из шаблона: {}", e, exc_info=True)
        await message.answer(
            t("admin.templates.create_sub_error", "❌ Произошла ошибка при создании подписки")
        )


@router.callback_query(F.data.startswith("template_add_inbound_"))
async def start_add_inbound_to_template(callback: CallbackQuery, state: FSMContext):
    """Start adding inbound to template - show multi-select for available inbounds."""
    template_id = int(callback.data.split("_")[3])

    async with async_session_factory() as session:
        template_service = SubscriptionTemplateService(session)
        template = await template_service.get_template(template_id)

        if not template:
            await callback.answer(
                t("admin.templates.not_found", "⚠️ Шаблон не найден"), show_alert=True
            )
            return

    # Get current template inbounds
    template_inbound_ids = await get_template_inbounds(template_id)

    # Show multi-select for adding inbounds
    await state.update_data(template_id=template_id, selected_inbound_ids=set())
    await state.set_state(TemplateManagement.waiting_for_inbound_selection)

    # Get all inbounds using same session
    from app.services.xui_service import XUIService

    async with async_session_factory() as session2:
        xui_service = XUIService(session2)
        inbounds = await xui_service.get_all_inbounds()

        # Filter out inbounds already in template
        template_inbound_ids = await get_template_inbounds(template_id)
        available_inbounds = [ib for ib in inbounds if ib.id not in template_inbound_ids]

        if not available_inbounds:
            await callback.answer(
                t(
                    "admin.templates.all_inbounds_added",
                    "⚠️ Все доступные подключения уже добавлены в шаблон",
                ),
                show_alert=True,
            )
            # Show current inbounds instead
            template_service = SubscriptionTemplateService(session2)
            template_inbounds = await template_service.get_template_inbounds(template_id)
            keyboard = get_template_inbounds_keyboard(template_id, template_inbounds)
            await callback.message.edit_text(
                t(
                    "admin.templates.manage_inbounds",
                    "🔄 <b>Управление подключениями шаблона</b>\n\n"
                    "📋 <b>{name}</b>\n"
                    "🔌 Подключений: {count}\n\n"
                    "Для управления подключениями используйте кнопки ниже:",
                    name=template.name,
                    count=len(template_inbounds),
                ),
                reply_markup=keyboard,
            )
            return

        keyboard = get_inbound_selection_for_template(template_id, available_inbounds)

    await callback.message.edit_text(
        t(
            "admin.templates.add_inbounds_prompt",
            "📋 <b>Добавление подключений к шаблону</b>\n\n"
            "📝 Шаблон: <b>{name}</b>\n"
            "🔌 Доступно подключений: {count}\n\n"
            "Выберите inbounds (можно выбрать несколько):\n"
            "Нажмите '➡️ Добавить выбранные' когда выбор готов:",
            name=template.name,
            count=len(available_inbounds),
        ),
        reply_markup=keyboard,
    )
    await callback.answer()


@router.callback_query(
    TemplateManagement.waiting_for_inbound_selection, F.data.startswith("template_toggle_inbound_")
)
async def toggle_inbound_selection_for_template(callback: CallbackQuery, state: FSMContext):
    """Toggle inbound selection in multi-select mode for adding to template."""
    parts = callback.data.split("_")
    template_id = int(parts[3])
    inbound_id = int(parts[4])

    data = await state.get_data()
    selected_inbound_ids = data.get("selected_inbound_ids", set())

    if inbound_id in selected_inbound_ids:
        selected_inbound_ids.remove(inbound_id)
    else:
        selected_inbound_ids.add(inbound_id)

    await state.update_data(selected_inbound_ids=selected_inbound_ids)

    # Get available inbounds and template inbound IDs
    from app.services.xui_service import XUIService

    async with async_session_factory() as session:
        xui_service = XUIService(session)
        inbounds = await xui_service.get_all_inbounds()

        # Filter out inbounds already in template
        template_inbound_ids = await get_template_inbounds(template_id)
        available_inbounds = [ib for ib in inbounds if ib.id not in template_inbound_ids]

        # Get template info
        template_service = SubscriptionTemplateService(session)
        await template_service.get_template(template_id)

    # Update keyboard with selection state
    builder = InlineKeyboardBuilder()

    # Group inbounds by server
    from collections import defaultdict

    inbounds_by_server = defaultdict(list)
    for inbound in available_inbounds:
        inbounds_by_server[inbound.server.name].append(inbound)

    for server_name, server_inbounds in sorted(inbounds_by_server.items()):
        for inbound in server_inbounds:
            status = "✅" if inbound.is_active else "❌"
            selected = "🔘" if inbound.id in selected_inbound_ids else "⭕"
            builder.button(
                text=f"{selected} {status} {inbound.remark} ({server_name})",
                callback_data=f"template_toggle_inbound_{template_id}_{inbound.id}",
            )

    builder.button(
        text=t("admin.templates.btn_add_selected", "➡️ Добавить выбранные"),
        callback_data="template_confirm_add_inbounds",
    )
    builder.button(
        text=t("admin.templates.btn_back", "Назад"), callback_data=f"template_select_{template_id}"
    )
    builder.adjust(1)

    await callback.message.edit_reply_markup(reply_markup=builder.as_markup())
    await callback.answer()


@router.callback_query(
    TemplateManagement.waiting_for_inbound_selection, F.data == "template_confirm_add_inbounds"
)
async def confirm_add_inbounds_to_template(callback: CallbackQuery, state: FSMContext):
    """Confirm and add selected inbounds to template."""
    data = await state.get_data()
    selected_inbound_ids = data.get("selected_inbound_ids", set())
    template_id = data["template_id"]

    if not selected_inbound_ids:
        await callback.answer(
            t("admin.templates.select_at_least_one_inbound", "❌ Выберите хотя бы один inbound."),
            show_alert=True,
        )
        return

    try:
        async with async_session_factory() as session:
            template_service = SubscriptionTemplateService(session)

            # Get current inbound IDs count for order
            template_inbound_ids = await get_template_inbounds(template_id)
            start_order = len(template_inbound_ids)

            # Add each selected inbound to template
            added_count = 0
            for order, inbound_id in enumerate(selected_inbound_ids, start=start_order):
                try:
                    await template_service.add_inbound_to_template(
                        template_id=template_id,
                        inbound_id=inbound_id,
                        order=order,
                    )
                    added_count += 1
                except Exception as e:
                    logger.warning("Не удалось добавить inbound {} в шаблон: {}", inbound_id, e)

            await session.commit()

            async def run_bg():
                try:
                    async with async_session_factory() as bg_session:
                        bg_service = SubscriptionTemplateService(bg_session)
                        await bg_service._apply_template_inbounds_change(
                            template_id,
                            added_inbound_ids=selected_inbound_ids,
                            removed_inbound_ids=set(),
                        )
                        await bg_session.commit()
                        logger.debug("Фоновая задача добавления inbound завершена")
                except Exception as e:
                    logger.warning("Фоновая задача добавления inbound завершилась с ошибкой: {}", e)

            asyncio.create_task(run_bg())

            # Get updated template info
            template = await template_service.get_template(template_id)
            await template_service.get_template_inbounds(template_id)

            logger.info("Добавлено {} inbound к шаблону {}", added_count, template.name)

        # Show success message and return to server selection
        await state.clear()
        await state.update_data(template_id=template_id)
        await state.set_state(TemplateManagement.waiting_for_server_selection)

        from app.services.xui_service import XUIService

        async with async_session_factory() as session:
            xui_service = XUIService(session)
            servers = await xui_service.get_active_servers()

        await callback.message.edit_text(
            t(
                "admin.templates.added_inbounds_success",
                "✅ <b>Успешно добавлено {count} подключений</b>\n\n"
                "✅ Шаблон изменен. Запущено фоновое обновление всех привязанных подписок...\n\n"
                "📝 Шаблон: <b>{name}</b>\n"
                "🔌 Всего подключений: {total_count}\n\n"
                "Выберите сервер для продолжения редактирования:",
                count=added_count,
                name=template.name,
                total_count=len(template.template_inbounds),
            ),
            reply_markup=get_servers_keyboard_for_template_edit(servers, template_id),
        )
        await callback.answer(
            t(
                "admin.templates.added_inbounds_alert",
                "✅ Добавлено {count} подключений",
                count=added_count,
            ),
            show_alert=True,
        )

    except XUIError as e:
        await state.clear()
        await callback.message.edit_text(
            t(
                "admin.templates.add_inbounds_error_xui",
                "❌ Ошибка при добавлении подключений: {error}",
                error=str(e),
            )
        )
        await callback.answer()
    except Exception as e:
        await state.clear()
        logger.error("Ошибка добавления inbound к шаблону: {}", e, exc_info=True)
        await callback.message.edit_text(
            t(
                "admin.templates.add_inbounds_error",
                "❌ Произошла ошибка при добавлении подключений",
            )
        )
        await callback.answer()


async def get_template_inbounds(template_id: int) -> set:
    """Helper function to get template inbound IDs.

    Args:
        template_id: Template ID

    Returns:
        Set of inbound IDs already in template
    """
    async with async_session_factory() as session:
        template_service = SubscriptionTemplateService(session)
        inbounds = await template_service.get_template_inbounds(template_id)
        inbound_ids = {ti.inbound_id for ti in inbounds}
        return inbound_ids


@router.callback_query(F.data.startswith("template_delete_"))
async def start_delete_template(callback: CallbackQuery, state: FSMContext):
    """Start template deletion."""
    template_id = int(callback.data.split("_")[2])

    async with async_session_factory() as session:
        template_service = SubscriptionTemplateService(session)
        template = await template_service.get_template(template_id)

        if not template:
            await callback.answer(
                t("admin.templates.not_found", "⚠️ Шаблон не найден"), show_alert=True
            )
            return

    await state.update_data(template_id=template_id, template_name=template.name)
    await state.set_state(TemplateManagement.confirm_delete_template)

    text = t(
        "admin.templates.delete_confirm",
        "❌ <b>Удаление шаблона</b>\n\n"
        "Вы уверены, что хотите удалить шаблон <b>{name}</b>?\n\n"
        "<b>Внимание!</b> Это действие необратимо. Все подписки, созданные с использованием этого шаблона, сохранятся.",
        name=template.name,
    )

    keyboard = get_confirm_keyboard("confirm_delete_template", "admin_templates")

    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data == "confirm_confirm_delete_template")
async def confirm_delete_template(callback: CallbackQuery, state: FSMContext):
    """Confirm and delete template."""
    data = await state.get_data()
    template_id = data["template_id"]

    try:
        async with async_session_factory() as session:
            template_service = SubscriptionTemplateService(session)
            deleted = await template_service.delete_template_safely(template_id)
            await session.commit()

        if deleted:
            logger.info("Шаблон '{}' удалён", data.get('template_name'))

        await state.clear()

        # Show templates list
        async with async_session_factory() as session:
            template_service = SubscriptionTemplateService(session)
            templates = await template_service.get_all_templates()

            if not templates:
                text = "📋 <b>Шаблоны подписок</b>\n\nШаблонов пока нет. Создайте первый шаблон!"
                keyboard = get_back_keyboard("admin_clients_menu")
            else:
                text = f"📋 <b>Шаблоны подписок</b>\n\nВсего шаблонов: {len(templates)}"
                keyboard = get_templates_keyboard(templates)

        await callback.message.edit_text(text, reply_markup=keyboard)
        await callback.answer(t("admin.templates.deleted_success", "✅ Шаблон удален"))

    except Exception as e:
        await state.clear()
        logger.error("Ошибка удаления шаблона: {}", e, exc_info=True)
        await callback.message.edit_text(
            t("admin.templates.delete_error", "❌ Произошла ошибка при удалении шаблона")
        )
        await callback.answer()


@router.callback_query(
    F.data.startswith("template_edit_") & ~F.data.startswith("template_edit_menu_")
)
async def start_edit_template(callback: CallbackQuery, state: FSMContext):
    """Start editing template."""
    parts = callback.data.split("_")

    # Validate callback data format
    if len(parts) < 4:
        await callback.answer(
            t("admin.templates.invalid_data_format", "⚠️ Неверный формат данных"), show_alert=True
        )
        return

    edit_field = parts[2]

    try:
        template_id = int(parts[3])
    except (ValueError, IndexError):
        await callback.answer(
            t("admin.templates.invalid_template_id", "⚠️ Неверный ID шаблона"), show_alert=True
        )
        return

    async with async_session_factory() as session:
        template_service = SubscriptionTemplateService(session)
        template = await template_service.get_template(template_id)

        if not template:
            await callback.answer(
                t("admin.templates.not_found", "⚠️ Шаблон не найден"), show_alert=True
            )
            return

    await state.update_data(template_id=template_id)

    # Map edit_field to state
    state_mapping = {
        "name": TemplateManagement.editing_template_name,
        "description": TemplateManagement.editing_template_description,
        "traffic": TemplateManagement.editing_default_traffic,
        "expiry": TemplateManagement.editing_default_expiry,
        "notes": TemplateManagement.editing_template_notes,
        "price": TemplateManagement.editing_template_price,
    }

    if edit_field not in state_mapping:
        await callback.answer(
            t("admin.templates.unknown_edit_field", "⚠️ Неизвестное поле для редактирования"),
            show_alert=True,
        )
        return

    if edit_field == "menu":
        # Show edit menu directly
        async with async_session_factory() as session:
            template_service = SubscriptionTemplateService(session)
            template = await template_service.get_template(template_id)

            if not template:
                await callback.answer("⚠️ Шаблон не найден", show_alert=True)
                return

        text = (
            f"✏️ <b>Редактирование шаблона</b>\n\n"
            f"📋 <b>{template.name}</b>\n\n"
            f"Выберите поле для редактирования:"
        )

        keyboard = get_template_edit_menu_keyboard(template_id)

        await callback.message.edit_text(text, reply_markup=keyboard)
        await callback.answer()
        return

    await state.set_state(state_mapping[edit_field])

    # Get current value message
    current_value_messages = {
        "name": t(
            "admin.templates.current_name", "Текущее название: <b>{name}</b>", name=template.name
        ),
        "description": t(
            "admin.templates.current_description",
            "Текущее описание: <b>{desc}</b>",
            desc=template.description or t("admin.templates.no_description", "Нет описания"),
        ),
        "traffic": t(
            "admin.templates.current_traffic",
            "Текущий лимит трафика: <b>{traffic} ГБ {unlimited}</b>",
            traffic=template.default_total_gb,
            unlimited=t("admin.templates.unlimited", "(безлимитный)")
            if template.default_total_gb == 0
            else "",
        ).strip(),
        "expiry": t(
            "admin.templates.current_expiry",
            "Текущий срок действия: <b>{count} дн.</b>",
            count=template.default_expiry_days,
        )
        if template.default_expiry_days
        else t("admin.templates.unlimited_time_capital_bold", "<b>Бессрочный</b>"),
        "notes": t(
            "admin.templates.current_notes",
            "Текущие заметки: <b>{notes}</b>",
            notes=template.notes or t("admin.templates.no_notes", "Нет заметок"),
        ),
        "price": t(
            "admin.templates.current_price",
            "Текущая цена: <b>{price}</b>",
            price=format_price(template.price_kopecks),
        ),
    }

    prompt_messages = {
        "name": t(
            "admin.templates.prompt_new_name",
            "Введите новое название (или /skip чтобы оставить текущее):",
        ),
        "description": t(
            "admin.templates.prompt_new_description",
            "Введите новое описание (или /skip чтобы оставить текущее):",
        ),
        "traffic": t(
            "admin.templates.prompt_new_traffic",
            "Введите новый лимит трафика в ГБ (0 = безлимитный, /skip = использовать текущее):",
        ),
        "expiry": t(
            "admin.templates.prompt_new_expiry",
            "Введите новый срок действия в днях (0 = бессрочный, /skip = использовать текущее):",
        ),
        "notes": t(
            "admin.templates.prompt_new_notes",
            "Введите новые заметки (или /skip чтобы оставить текущие):",
        ),
        "price": t(
            "admin.templates.prompt_new_price",
            "Введите цену за 30 дней в рублях, например <code>349.90</code>.\n"
            "0 = бесплатно, /clear = убрать цену, /skip = оставить текущую:",
        ),
    }

    text = t(
        "admin.templates.edit_field_header",
        "✏️ <b>Редактирование: {current_value}</b>\n\n{prompt}",
        current_value=current_value_messages[edit_field]
        .replace("Текущее ", "")
        .replace("Текущий ", "")
        .replace("Текущие ", ""),
        prompt=prompt_messages[edit_field],
    )

    keyboard = get_back_keyboard(f"template_edit_menu_{template_id}")

    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("template_edit_menu_"))
async def show_template_edit_menu(callback: CallbackQuery, state: FSMContext):
    """Show template edit menu with field options."""
    parts = callback.data.split("_")

    # Validate callback data format
    if len(parts) < 4:
        await callback.answer(
            t("admin.templates.invalid_data_format", "⚠️ Неверный формат данных"), show_alert=True
        )
        return

    try:
        template_id = int(parts[3])
    except (ValueError, IndexError):
        await callback.answer(
            t("admin.templates.invalid_template_id", "⚠️ Неверный ID шаблона"), show_alert=True
        )
        return

    # Clear any existing editing state
    await state.clear()

    async with async_session_factory() as session:
        template_service = SubscriptionTemplateService(session)
        template = await template_service.get_template(template_id)

        if not template:
            await callback.answer(
                t("admin.templates.not_found", "⚠️ Шаблон не найден"), show_alert=True
            )
            return

    text = t(
        "admin.templates.edit_menu_title",
        "✏️ <b>Редактирование шаблона</b>\n\n📋 <b>{name}</b>\n\nВыберите поле для редактирования:",
        name=template.name,
    )

    keyboard = get_template_edit_menu_keyboard(template_id)

    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@router.message(TemplateManagement.editing_template_name)
async def process_edit_template_name(message: Message, state: FSMContext):
    """Process template name edit."""
    new_name = message.text.strip()

    if new_name == "/skip":
        await show_template_details_edit_menu(message, state)
        return

    if not new_name:
        await message.answer(
            t("admin.templates.name_empty", "⚠️ Название не может быть пустым. Введите название:")
        )
        return

    if len(new_name) > 100:
        await message.answer(
            t(
                "admin.templates.name_too_long",
                "⚠️ Название слишком длинное. Максимум 100 символов. Введите название:",
            )
        )
        return

    data = await state.get_data()
    template_id = data["template_id"]

    try:
        async with async_session_factory() as session:
            template_service = SubscriptionTemplateService(session)
            template = await template_service.update_template(
                template_id=template_id,
                name=new_name,
            )
            await session.commit()

        logger.info("Название шаблона обновлено: {}", template.name)

        await message.answer(
            t("admin.templates.name_updated", "✅ Название изменено на: {name}", name=new_name)
        )
        await show_template_details_edit_menu(message, state)

    except Exception as e:
        logger.error("Ошибка обновления названия шаблона: {}", e, exc_info=True)
        await message.answer(
            t("admin.templates.update_name_error", "❌ Произошла ошибка при изменении названия")
        )
        await show_template_details_edit_menu(message, state)


@router.message(TemplateManagement.editing_template_description)
async def process_edit_template_description(message: Message, state: FSMContext):
    """Process template description edit."""
    new_description = message.text.strip()

    if new_description == "/skip":
        await show_template_details_edit_menu(message, state)
        return

    if len(new_description) > 500:
        await message.answer(
            t(
                "admin.templates.desc_too_long",
                "⚠️ Описание слишком длинное. Максимум 500 символов. Введите описание:",
            )
        )
        return

    data = await state.get_data()
    template_id = data["template_id"]

    try:
        async with async_session_factory() as session:
            template_service = SubscriptionTemplateService(session)
            template = await template_service.update_template(
                template_id=template_id,
                description=new_description,
            )
            await session.commit()

        logger.info("Описание шаблона обновлено: {}", template.name)

        await message.answer(t("admin.templates.desc_updated", "✅ Описание изменено"))
        await show_template_details_edit_menu(message, state)

    except Exception as e:
        logger.error("Ошибка обновления описания шаблона: {}", e, exc_info=True)
        await message.answer(
            t("admin.templates.edit_error", "❌ Произошла ошибка при обновлении шаблона")
        )
        await show_template_details_edit_menu(message, state)


@router.message(TemplateManagement.editing_default_traffic)
async def process_edit_default_traffic(message: Message, state: FSMContext):
    """Process default traffic edit."""
    traffic_input = message.text.strip()

    if traffic_input == "/skip":
        await show_template_details_edit_menu(message, state)
        return

    try:
        traffic = int(traffic_input)
        if traffic < 0:
            await message.answer(
                t(
                    "admin.templates.traffic_negative",
                    "⚠️ Лимит трафика не может быть отрицательным. Введите число:",
                )
            )
            return
    except ValueError:
        await message.answer(
            t(
                "admin.templates.prompt_new_traffic",
                "⚠️ Введите корректное число (0 = безлимитный, /skip = использовать текущее):",
            )
        )
        return

    data = await state.get_data()
    template_id = data["template_id"]

    try:
        async with async_session_factory() as session:
            template_service = SubscriptionTemplateService(session)

            old_template = await template_service.get_template(template_id)
            if not old_template:
                await message.answer(t("admin.templates.not_found", "⚠️ Шаблон не найден"))
                await show_template_details_edit_menu(message, state)
                return
            old_gb = old_template.default_total_gb
            old_days = old_template.default_expiry_days

            template = await template_service.update_template(
                template_id=template_id,
                default_total_gb=traffic,
            )
            await session.commit()

            async def run_bg():
                try:
                    async with async_session_factory() as bg_session:
                        bg_service = SubscriptionTemplateService(bg_session)
                        await bg_service._apply_template_limits_change(
                            template_id, old_gb, traffic, old_days, old_days
                        )
                        await bg_session.commit()
                        logger.debug("Фоновая задача обновления трафика завершена")
                except Exception as e:
                    logger.warning("Фоновая задача обновления трафика завершилась с ошибкой: {}", e)

            asyncio.create_task(run_bg())

        logger.info("Лимит трафика шаблона обновлён: {}", template.name)

        traffic_text = t(
            "admin.templates.traffic_format",
            "{traffic} ГБ {unlimited}",
            traffic=traffic,
            unlimited=t("admin.templates.unlimited", "(безлимитный)") if traffic == 0 else "",
        ).strip()
        await message.answer(
            t(
                "admin.templates.traffic_updated",
                "✅ Лимит трафика изменен на: {traffic}\n\n✅ Шаблон изменен. Запущено фоновое обновление всех привязанных подписок...",
                traffic=traffic_text,
            )
        )
        await show_template_details_edit_menu(message, state)

    except Exception as e:
        logger.error("Ошибка обновления трафика шаблона: {}", e, exc_info=True)
        await message.answer(
            t(
                "admin.templates.update_traffic_error",
                "❌ Произошла ошибка при изменении лимита трафика",
            )
        )
        await show_template_details_edit_menu(message, state)


@router.message(TemplateManagement.editing_default_expiry)
async def process_edit_default_expiry(message: Message, state: FSMContext):
    """Process default expiry edit."""
    expiry_input = message.text.strip()

    if expiry_input == "/skip":
        await show_template_details_edit_menu(message, state)
        return

    try:
        expiry = int(expiry_input)
        if expiry < 0:
            await message.answer(
                t(
                    "admin.templates.expiry_negative",
                    "⚠️ Срок действия не может быть отрицательным. Введите число:",
                )
            )
            return
    except ValueError:
        await message.answer(
            t(
                "admin.templates.prompt_new_expiry",
                "⚠️ Введите корректное число (0 = бессрочный, /skip = использовать текущее):",
            )
        )
        return

    if expiry == 0:
        expiry = None

    data = await state.get_data()
    template_id = data["template_id"]

    try:
        async with async_session_factory() as session:
            template_service = SubscriptionTemplateService(session)

            old_template = await template_service.get_template(template_id)
            if not old_template:
                await message.answer(t("admin.templates.not_found", "⚠️ Шаблон не найден"))
                await show_template_details_edit_menu(message, state)
                return
            old_gb = old_template.default_total_gb
            old_days = old_template.default_expiry_days

            template = await template_service.update_template(
                template_id=template_id,
                default_expiry_days=expiry,
            )
            await session.commit()

            async def run_bg():
                try:
                    async with async_session_factory() as bg_session:
                        bg_service = SubscriptionTemplateService(bg_session)
                        await bg_service._apply_template_limits_change(
                            template_id, old_gb, old_gb, old_days, expiry
                        )
                        await bg_session.commit()
                        logger.debug("Фоновая задача обновления срока завершена")
                except Exception as e:
                    logger.warning("Фоновая задача обновления срока завершилась с ошибкой: {}", e)

            asyncio.create_task(run_bg())

        logger.info("Срок действия шаблона обновлён: {}", template.name)

        expiry_text = (
            t("admin.templates.days_format", "{count} дн.", count=expiry)
            if expiry
            else t("admin.templates.unlimited_time_capital", "Бессрочный")
        )
        await message.answer(
            t(
                "admin.templates.expiry_updated",
                "✅ Срок действия изменен на: {expiry}\n\n✅ Шаблон изменен. Запущено фоновое обновление всех привязанных подписок...",
                expiry=expiry_text,
            )
        )
        await show_template_details_edit_menu(message, state)

    except Exception as e:
        logger.error("Ошибка обновления срока шаблона: {}", e, exc_info=True)
        await message.answer(
            t(
                "admin.templates.update_expiry_error",
                "❌ Произошла ошибка при изменении срока действия",
            )
        )
        await show_template_details_edit_menu(message, state)


@router.message(TemplateManagement.editing_template_notes)
async def process_edit_template_notes(message: Message, state: FSMContext):
    """Process template notes edit."""
    new_notes = message.text.strip()

    if new_notes == "/skip":
        await show_template_details_edit_menu(message, state)
        return

    if len(new_notes) > 500:
        await message.answer(
            t(
                "admin.templates.notes_too_long",
                "⚠️ Заметки слишком длинные. Максимум 500 символов. Введите заметки:",
            )
        )
        return

    data = await state.get_data()
    template_id = data["template_id"]

    try:
        async with async_session_factory() as session:
            template_service = SubscriptionTemplateService(session)
            template = await template_service.update_template(
                template_id=template_id,
                notes=new_notes,
            )
            await session.commit()

        logger.info("Заметки шаблона обновлены: {}", template.name)

        await message.answer(t("admin.templates.notes_updated", "✅ Заметки изменены"))
        await show_template_details_edit_menu(message, state)

    except Exception as e:
        logger.error("Ошибка обновления заметок шаблона: {}", e, exc_info=True)
        await message.answer(
            t("admin.templates.update_notes_error", "❌ Произошла ошибка при изменении заметок")
        )
        await show_template_details_edit_menu(message, state)


@router.message(TemplateManagement.editing_template_price)
async def process_edit_template_price(message: Message, state: FSMContext):
    """Обработать ввод цены шаблона.

    ``/clear`` убирает цену (её унаследуют подписки как «не задана»),
    ``/skip`` оставляет текущую.
    """
    raw = (message.text or "").strip()

    if raw == "/skip":
        await show_template_details_edit_menu(message, state)
        return

    if raw == "/clear":
        new_price = None
    else:
        try:
            new_price = parse_price_kopecks(raw)
        except ValueError as e:
            await message.answer(
                t(
                    "admin.templates.price_invalid",
                    "⚠️ {error}\nВведите цену в рублях, например <code>349.90</code>:",
                    error=html.escape(str(e)),
                )
            )
            return

    data = await state.get_data()
    template_id = data["template_id"]

    try:
        async with async_session_factory() as session:
            if not await set_template_price(session, template_id, new_price):
                await message.answer(t("admin.templates.not_found", "⚠️ Шаблон не найден"))
                await show_template_details_edit_menu(message, state)
                return
            await session.commit()

        logger.info("Цена шаблона {} обновлена: {}", template_id, format_price(new_price))
        await message.answer(
            t("admin.templates.price_updated", "✅ Цена: {price}", price=format_price(new_price))
        )
    except Exception as e:
        logger.error("Ошибка обновления цены шаблона: {}", e, exc_info=True)
        await message.answer(
            t("admin.templates.update_price_error", "❌ Не удалось изменить цену")
        )

    await show_template_details_edit_menu(message, state)


async def show_template_details_edit_menu(message: Message, state: FSMContext):
    """Show template edit menu after editing a field."""
    data = await state.get_data()
    template_id = data["template_id"]

    # Clear editing state
    await state.clear()
    await state.update_data(template_id=template_id)

    async with async_session_factory() as session:
        template_service = SubscriptionTemplateService(session)
        template = await template_service.get_template(template_id)

        if not template:
            await message.answer("⚠️ Шаблон не найден")
            return

    text = t(
        "admin.templates.edit_menu_title",
        "✏️ <b>Редактирование шаблона</b>\n\n📋 <b>{name}</b>\n\nВыберите поле для редактирования:",
        name=template.name,
    )

    keyboard = get_template_edit_menu_keyboard(template_id)

    await message.answer(text, reply_markup=keyboard)


@router.callback_query(F.data.startswith("template_inbound_remove_"))
async def start_remove_inbound_from_template(
    callback: CallbackQuery, state: FSMContext
):
    """Start removing inbound from template."""
    parts = callback.data.split("_")
    template_id = int(parts[3])
    inbound_id = int(parts[4])

    async with async_session_factory() as session:
        template_service = SubscriptionTemplateService(session)
        template = await template_service.get_template(template_id)
        template_inbound = await template_service.get_template_inbound(template_id, inbound_id)

        if not template or not template_inbound:
            await callback.answer("⚠️ Шаблон или подключение не найдены", show_alert=True)
            return

    await state.update_data(template_id=template_id, inbound_id=inbound_id)
    await state.set_state(TemplateManagement.confirm_remove_inbound)

    text = t(
        "admin.templates.remove_inbound_confirm",
        "❌ <b>Удаление подключения из шаблона</b>\n\n"
        "Вы уверены, что хотите удалить подключение <b>{remark}</b> из шаблона <b>{name}</b>?\n\n"
        "<b>Внимание!</b> Это действие необратимо.",
        remark=template_inbound.inbound.remark,
        name=template.name,
    )

    keyboard = get_confirm_keyboard("confirm_remove_inbound", f"template_select_{template_id}")

    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data == "confirm_confirm_remove_inbound")
async def confirm_remove_inbound(callback: CallbackQuery, state: FSMContext):
    """Confirm and remove inbound from template."""
    data = await state.get_data()
    template_id = data["template_id"]
    inbound_id = data["inbound_id"]

    try:
        async with async_session_factory() as session:
            template_service = SubscriptionTemplateService(session)
            removed = await template_service.remove_inbound_from_template(template_id, inbound_id)
            await session.commit()

        if removed:

            async def run_bg():
                try:
                    async with async_session_factory() as bg_session:
                        bg_service = SubscriptionTemplateService(bg_session)
                        await bg_service._apply_template_inbounds_change(
                            template_id, added_inbound_ids=set(), removed_inbound_ids={inbound_id}
                        )
                        await bg_session.commit()
                        logger.debug("Фоновая задача удаления inbound завершена")
                except Exception as e:
                    logger.warning("Фоновая задача удаления inbound завершилась с ошибкой: {}", e)

            asyncio.create_task(run_bg())

            logger.info("Inbound {} удалён из шаблона {}", inbound_id, template_id)

        await state.clear()

        # Show updated template details
        async with async_session_factory() as session:
            template_service = SubscriptionTemplateService(session)
            template = await template_service.get_template(template_id)

            traffic_limit = (
                f"{template.default_total_gb} ГБ"
                if template.default_total_gb > 0
                else "Безлимитный"
            )
            expiry_text = (
                f"{template.default_expiry_days} дн."
                if template.default_expiry_days
                else "Бессрочный"
            )
            inbounds_count = len(template.template_inbounds)

            text = (
                f"📋 <b>{template.name}</b>\n\n"
                f"📝 {template.description or 'Нет описания'}\n"
                f"📊 <b>Лимит трафика:</b> {traffic_limit}\n"
                f"📅 <b>Срок действия:</b> {expiry_text}\n"
                f"🔌 <b>Подключений:</b> {inbounds_count}\n"
            )

            if template.notes:
                text += f"\n📌 <b>Заметки:</b> {template.notes}"

            text += t(
                "admin.templates.bg_update_started",
                "\n\n✅ Шаблон изменен. Запущено фоновое обновление всех привязанных подписок...",
            )

            keyboard = get_template_actions_keyboard(template.id, template.is_public)

        await callback.message.edit_text(text, reply_markup=keyboard)
        await callback.answer(
            t(
                "admin.templates.inbound_removed_alert",
                "✅ Подключение удалено. Запущено фоновое обновление...",
            ),
            show_alert=True,
        )

    except Exception as e:
        await state.clear()
        logger.error("Ошибка удаления inbound из шаблона: {}", e, exc_info=True)
        await callback.message.edit_text(
            t(
                "admin.templates.remove_inbound_error",
                "❌ Произошла ошибка при удалении подключения",
            )
        )
        await callback.answer()


@router.callback_query(F.data.startswith("template_client_search_"))
async def start_template_client_search(callback: CallbackQuery, state: FSMContext):
    """Start client search for template subscription creation."""
    parts = callback.data.split("_")
    template_id = int(parts[3])

    await state.update_data(template_id=template_id)
    await state.set_state(TemplateManagement.waiting_for_search_query)

    keyboard = get_back_keyboard(f"template_select_{template_id}")

    await callback.message.edit_text(
        t(
            "admin.templates.search_client_prompt",
            "🔍 <b>Поиск клиента</b>\n\nВведите имя, email или Telegram ID клиента для поиска:",
        ),
        reply_markup=keyboard,
    )
    await callback.answer()


@router.message(TemplateManagement.waiting_for_search_query)
async def process_template_client_search(message: Message, state: FSMContext):
    """Process client search for template subscription creation."""
    query = message.text.strip()

    if not query:
        await message.answer(
            t(
                "admin.templates.search_empty",
                "⚠️ Поисковый запрос не может быть пустым. Введите запрос:",
            )
        )
        return

    try:
        async with async_session_factory() as session:
            client_service = ClientService(session)
            all_results = await client_service.search_clients_all_fields(query)
            clients = [c for c in all_results if c.is_active][:10]

        if not clients:
            await message.answer(
                t(
                    "admin.templates.search_not_found",
                    "❌ Клиенты не найдены по запросу: <b>{query}</b>\n\n"
                    "Попробуйте другой запрос или создайте нового клиента.",
                    query=query,
                ),
                parse_mode="HTML",
            )
            return

        data = await state.get_data()
        template_id = data.get("template_id")
        await state.set_state(TemplateManagement.waiting_for_client_selection)

        text = t(
            "admin.templates.search_results",
            "🔍 <b>Результаты поиска</b>\n\nПо запросу: <b>{query}</b>\n\nНайдено клиентов: {count}\n\nВыберите клиента:",
            query=query,
            count=len(clients),
        )

        from aiogram.utils.keyboard import InlineKeyboardBuilder

        builder = InlineKeyboardBuilder()
        for client in clients:
            builder.button(
                text=f"👤 {client.name}",
                callback_data=f"template_client_select_{client.id}",
            )
        builder.button(
            text=t("admin.templates.btn_new_search", "🔍 Новый поиск"),
            callback_data=f"template_client_search_{template_id}",
        )
        builder.button(
            text=t("admin.templates.btn_back", "Назад"),
            callback_data=f"template_select_{template_id}",
        )
        builder.adjust(1)

        await message.answer(text, reply_markup=builder.as_markup())

    except Exception as e:
        logger.error("Ошибка поиска клиентов: {}", e, exc_info=True)
        await message.answer(
            t("admin.templates.search_error", "❌ Произошла ошибка при поиске клиентов")
        )


@router.callback_query(F.data == "template_no_inbounds")
async def handle_template_no_inbounds(callback: CallbackQuery):
    """Handle template no inbounds button click."""
    await callback.answer(
        t(
            "admin.templates.no_inbounds_alert",
            "⚠️ В шаблоне нет подключений. Добавьте подключения.",
        ),
        show_alert=True,
    )


@router.callback_query(F.data.startswith("template_multi_select_mode_"))
async def enter_template_multi_select_mode(
    callback: CallbackQuery, state: FSMContext
):
    """Enter multi-select mode for template inbounds."""
    template_id = int(callback.data.split("_")[4])
    await state.update_data(template_id=template_id, selected_inbound_ids=set())
    await state.set_state(TemplateManagement.inbounds_multi_select_mode)

    # Get template inbounds
    async with async_session_factory() as session:
        template_service = SubscriptionTemplateService(session)
        template_inbounds = await template_service.get_template_inbounds(template_id)
        template = await template_service.get_template(template_id)

    if not template_inbounds:
        builder = InlineKeyboardBuilder()
        builder.button(
            text=t("admin.templates.btn_back_arrow", "🔙 Назад"),
            callback_data=f"template_select_{template_id}",
        )
        builder.adjust(1)
        await callback.message.edit_text(
            t("admin.templates.template_has_no_inbounds", "❌ У шаблона нет подключений."),
            reply_markup=builder.as_markup(),
        )
        await callback.answer()
        return

    await callback.message.edit_text(
        t(
            "admin.templates.multi_select_header",
            "✅ <b>Режим множественного выбора</b>\n\n"
            "📝 Шаблон: <b>{name}</b>\n"
            "Выберите inbounds для массовых действий:\n"
            "(Выбрано: 0/{count})",
            name=template.name,
            count=len(template_inbounds),
        ),
        reply_markup=get_template_multi_select_keyboard(template_id, template_inbounds, set()),
    )
    await callback.answer()


@router.callback_query(
    TemplateManagement.inbounds_multi_select_mode, F.data.startswith("template_multi_select_")
)
async def toggle_template_multi_selection(callback: CallbackQuery, state: FSMContext):
    """Toggle inbound selection in template multi-select mode."""
    parts = callback.data.split("_")

    # Skip the confirm button handler
    if len(parts) < 5:
        return

    template_id = int(parts[4])
    inbound_id = int(parts[5])

    data = await state.get_data()
    selected_inbound_ids = data.get("selected_inbound_ids", set())

    if inbound_id in selected_inbound_ids:
        selected_inbound_ids.remove(inbound_id)
    else:
        selected_inbound_ids.add(inbound_id)

    await state.update_data(selected_inbound_ids=selected_inbound_ids)

    # Get template inbounds
    async with async_session_factory() as session:
        template_service = SubscriptionTemplateService(session)
        template_inbounds = await template_service.get_template_inbounds(template_id)

    await callback.message.edit_text(
        t(
            "admin.templates.multi_select_toggle",
            "✅ <b>Режим множественного выбора</b>\n\n"
            "Выберите inbounds для массовых действий:\n"
            "(Выбрано: {selected}/{total})",
            selected=len(selected_inbound_ids),
            total=len(template_inbounds),
        ),
        reply_markup=get_template_multi_select_keyboard(
            template_id, template_inbounds, selected_inbound_ids
        ),
    )
    await callback.answer()


@router.callback_query(
    TemplateManagement.inbounds_multi_select_mode, F.data == "template_multi_delete_inbounds"
)
async def delete_selected_template_inbounds(callback: CallbackQuery, state: FSMContext):
    """Delete all selected inbounds from template."""
    data = await state.get_data()
    selected_inbound_ids = data.get("selected_inbound_ids", set())

    if not selected_inbound_ids:
        await callback.answer(
            t("admin.templates.select_at_least_one_inbound", "❌ Выберите хотя бы один inbound."),
            show_alert=True,
        )
        return

    await state.update_data(action="delete")
    await state.set_state(TemplateManagement.inbounds_multi_confirm_action)

    await callback.message.edit_text(
        t(
            "admin.templates.multi_delete_confirm",
            "⚠️ Удалить {count} подключений из шаблона?\n\nВсе выбранные inbounds будут удалены.",
            count=len(selected_inbound_ids),
        ),
        reply_markup=get_template_multi_select_confirm_keyboard(),
    )
    await callback.answer()


@router.callback_query(
    TemplateManagement.inbounds_multi_confirm_action, F.data == "template_multi_confirm"
)
async def confirm_template_multi_action(callback: CallbackQuery, state: FSMContext):
    """Confirm multi-select action for template."""
    data = await state.get_data()
    selected_inbound_ids = data.get("selected_inbound_ids", set())
    action = data.get("action")
    template_id = data["template_id"]

    if not selected_inbound_ids or not action:
        await callback.answer(
            t(
                "admin.templates.multi_action_error",
                "❌ Ошибка: нет выбранных подключений или действия.",
            ),
            show_alert=True,
        )
        await state.clear()
        return

    if action == "delete":
        try:
            async with async_session_factory() as session:
                template_service = SubscriptionTemplateService(session)

                deleted_count = 0
                for inbound_id in selected_inbound_ids:
                    try:
                        await template_service.remove_inbound_from_template(template_id, inbound_id)
                        deleted_count += 1
                    except Exception as e:
                        logger.warning("Не удалось удалить inbound {} из шаблона: {}", inbound_id, e)

                await session.commit()

                # Запускаем фоновое обновление для привязанных подписок
                async def run_bg():
                    try:
                        async with async_session_factory() as bg_session:
                            bg_service = SubscriptionTemplateService(bg_session)
                            await bg_service._apply_template_inbounds_change(
                                template_id,
                                added_inbound_ids=set(),
                                removed_inbound_ids=set(selected_inbound_ids),
                            )
                            await bg_session.commit()
                            logger.debug("Фоновая задача массового удаления inbound завершена")
                    except Exception as e:
                        logger.warning("Фоновая задача массового удаления inbound завершилась с ошибкой: {}", e)

                asyncio.create_task(run_bg())

                # Get updated template info
                template = await template_service.get_template(template_id)

            await state.clear()

            traffic_limit = (
                f"{template.default_total_gb} ГБ"
                if template.default_total_gb > 0
                else "Безлимитный"
            )
            expiry_text = (
                f"{template.default_expiry_days} дн."
                if template.default_expiry_days
                else "Бессрочный"
            )
            inbounds_count = len(template.template_inbounds)

            text = (
                f"📋 <b>{template.name}</b>\n\n"
                f"📝 {template.description or 'Нет описания'}\n"
                f"📊 <b>Лимит трафика:</b> {traffic_limit}\n"
                f"📅 <b>Срок действия:</b> {expiry_text}\n"
                f"🔌 <b>Подключений:</b> {inbounds_count}\n"
            )

            if template.notes:
                text += f"\n📌 <b>Заметки:</b> {template.notes}"

            keyboard = get_template_actions_keyboard(template.id, template.is_public)

            await callback.message.edit_text(text, reply_markup=keyboard)
            await callback.answer(
                t(
                    "admin.templates.multi_deleted_alert",
                    "✅ Удалено {count} подключений",
                    count=deleted_count,
                ),
                show_alert=True,
            )

        except Exception as e:
            logger.error("Ошибка массового удаления inbound из шаблона: {}", e, exc_info=True)
            await callback.answer(
                t("admin.templates.error_prefix", "❌ Ошибка: {error}", error=str(e)),
                show_alert=True,
            )
            await callback.message.edit_text(
                t(
                    "admin.templates.multi_delete_error",
                    "❌ Ошибка при удалении подключений: {error}",
                    error=str(e),
                ),
                reply_markup=get_back_keyboard(f"template_select_{template_id}"),
            )
            await state.clear()


@router.callback_query(
    TemplateManagement.inbounds_multi_confirm_action, F.data == "template_multi_cancel"
)
async def cancel_template_multi_action(callback: CallbackQuery, state: FSMContext):
    """Cancel multi-select action and return to selection mode."""
    data = await state.get_data()
    template_id = data["template_id"]

    async with async_session_factory() as session:
        template_service = SubscriptionTemplateService(session)
        template_inbounds = await template_service.get_template_inbounds(template_id)

    selected_inbound_ids = data.get("selected_inbound_ids", set())

    await state.set_state(TemplateManagement.inbounds_multi_select_mode)

    await callback.message.edit_text(
        t(
            "admin.templates.multi_select_toggle",
            "✅ <b>Режим множественного выбора</b>\n\n"
            "Выберите inbounds для массовых действий:\n"
            "(Выбрано: {selected}/{total})",
            selected=len(selected_inbound_ids),
            total=len(template_inbounds),
        ),
        reply_markup=get_template_multi_select_keyboard(
            template_id, template_inbounds, selected_inbound_ids
        ),
    )
    await callback.answer()


@router.callback_query(
    TemplateManagement.inbounds_multi_select_mode, F.data == "template_multi_cancel"
)
async def exit_template_multi_select_mode(callback: CallbackQuery, state: FSMContext):
    """Exit multi-select mode and show template details."""
    data = await state.get_data()
    template_id = data["template_id"]

    await state.clear()

    async with async_session_factory() as session:
        template_service = SubscriptionTemplateService(session)
        template = await template_service.get_template(template_id)
        await template_service.get_template_inbounds(template_id)

    traffic_limit = (
        f"{template.default_total_gb} ГБ" if template.default_total_gb > 0 else "Безлимитный"
    )
    expiry_text = (
        f"{template.default_expiry_days} дн." if template.default_expiry_days else "Бессрочный"
    )
    inbounds_count = len(template.template_inbounds)

    text = (
        f"📋 <b>{template.name}</b>\n\n"
        f"📝 {template.description or 'Нет описания'}\n"
        f"📊 <b>Лимит трафика:</b> {traffic_limit}\n"
        f"📅 <b>Срок действия:</b> {expiry_text}\n"
        f"🔌 <b>Подключений:</b> {inbounds_count}\n"
    )

    if template.notes:
        text += f"\n📌 <b>Заметки:</b> {template.notes}"

    keyboard = get_template_actions_keyboard(template.id)

    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


# Обработчики для редактирования подключений шаблона
@router.callback_query(F.data.startswith("template_manage_inbounds_"))
async def start_edit_template_inbounds(callback: CallbackQuery, state: FSMContext):
    """Start editing template inbounds - show servers list."""
    template_id = int(callback.data.split("_")[3])

    async with async_session_factory() as session:
        template_service = SubscriptionTemplateService(session)
        template = await template_service.get_template(template_id)

        if not template:
            await callback.answer(
                t("admin.templates.not_found", "⚠️ Шаблон не найден"), show_alert=True
            )
            return

    from app.services.xui_service import XUIService

    async with async_session_factory() as session2:
        xui_service = XUIService(session2)
        servers = await xui_service.get_active_servers()

    if not servers:
        await callback.answer(
            t("admin.templates.no_active_servers", "⚠️ Нет активных серверов"), show_alert=True
        )
        return

    await state.update_data(template_id=template_id)
    await state.set_state(TemplateManagement.waiting_for_server_selection)

    await callback.message.edit_text(
        t(
            "admin.templates.edit_inbounds_prompt",
            "✏️ <b>Редактирование подключений шаблона</b>\n\n"
            "📝 Шаблон: <b>{name}</b>\n\n"
            "Выберите сервер:",
            name=template.name,
        ),
        reply_markup=get_servers_keyboard_for_template_edit(servers, template_id),
    )
    await callback.answer()


@router.callback_query(
    TemplateManagement.waiting_for_server_selection,
    F.data.startswith("server_template_edit_server_"),
)
async def select_server_for_template_edit(callback: CallbackQuery, state: FSMContext):
    """Handle server selection for template inbound editing."""
    server_id = int(callback.data.split("_")[-1])
    await state.update_data(server_id=server_id)

    data = await state.get_data()
    template_id = data["template_id"]

    # Get all needed data in one session context
    async with async_session_factory() as session:
        from app.services.xui_service import XUIService

        xui_service = XUIService(session)

        # Get template inbounds
        template_inbound_ids = await get_template_inbounds(template_id)

        # Get inbounds for selected server - use XUI service from same session
        inbounds = await xui_service.get_server_inbounds(server_id)

        # Get server name
        server = await xui_service.get_server_by_id(server_id)

    if not inbounds:
        await callback.answer(
            t("admin.templates.server_has_no_inbounds", "⚠️ У сервера нет подключений"),
            show_alert=True,
        )
        # Return to server selection
        async with async_session_factory() as session:
            from app.services.xui_service import XUIService

            xui_service = XUIService(session)
            servers = await xui_service.get_active_servers()
        await state.set_state(TemplateManagement.waiting_for_server_selection)
        await callback.message.edit_text(
            t(
                "admin.templates.edit_inbounds_select_server",
                "✏️ <b>Редактирование подключений шаблона</b>\n\nВыберите сервер:",
            ),
            reply_markup=get_servers_keyboard_for_template_edit(servers, template_id),
        )
        return

    # Initialize selection state
    await state.update_data(template_inbound_ids=template_inbound_ids, selected_inbound_ids=set())
    await state.set_state(TemplateManagement.waiting_for_inbound_selection)

    keyboard = get_template_edit_inbounds_keyboard(
        template_id, inbounds, template_inbound_ids, set(), server.name
    )

    await callback.message.edit_text(
        t(
            "admin.templates.edit_inbounds_server",
            "✏️ <b>Редактирование подключений</b>\n\n"
            "🖥️ Сервер: <b>{server_name}</b>\n\n"
            "Выберите inbounds, затем нажмите кнопку действия:\n"
            "✅ - уже в шаблоне\n"
            "❌ - не в шаблоне",
            server_name=server.name,
        ),
        reply_markup=keyboard,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("template_bulk_cancel_"))
async def cancel_template_edit_inbounds(callback: CallbackQuery, state: FSMContext):
    """Cancel template inbounds editing and return to server selection."""
    parts = callback.data.split("_")
    template_id = int(parts[3])

    data = await state.get_data()
    data["template_id"] = template_id  # Ensure template_id is in state

    await state.update_data(template_id=template_id)

    # Return to server selection
    await state.clear()
    await state.update_data(template_id=template_id)
    await state.set_state(TemplateManagement.waiting_for_server_selection)

    from app.services.xui_service import XUIService

    async with async_session_factory() as session:
        xui_service = XUIService(session)
        servers = await xui_service.get_active_servers()
        template_service = SubscriptionTemplateService(session)
        template = await template_service.get_template(template_id)

    await callback.message.edit_text(
        t(
            "admin.templates.edit_inbounds_prompt",
            "✏️ <b>Редактирование подключений шаблона</b>\n\n"
            "📝 Шаблон: <b>{name}</b>\n\n"
            "Выберите сервер:",
            name=template.name,
        ),
        reply_markup=get_servers_keyboard_for_template_edit(servers, template_id),
    )
    await callback.answer()


@router.callback_query(
    TemplateManagement.waiting_for_inbound_selection,
    F.data.startswith("template_inbound_edit_toggle_"),
)
async def toggle_edit_inbound_selection(callback: CallbackQuery, state: FSMContext):
    """Toggle inbound selection in edit mode."""
    parts = callback.data.split("_")
    template_id = int(parts[4])
    inbound_id = int(parts[5])

    data = await state.get_data()
    selected_inbound_ids = data.get("selected_inbound_ids", set())
    template_inbound_ids = data.get("template_inbound_ids", set())

    if inbound_id in selected_inbound_ids:
        selected_inbound_ids.remove(inbound_id)
    else:
        selected_inbound_ids.add(inbound_id)

    await state.update_data(selected_inbound_ids=selected_inbound_ids)

    # Get inbounds for the selected server
    from app.services.xui_service import XUIService

    async with async_session_factory() as session:
        xui_service = XUIService(session)
        server_id = data.get("server_id")
        inbounds = await xui_service.get_server_inbounds(server_id)
        server = await xui_service.get_server_by_id(server_id)

    keyboard = get_template_edit_inbounds_keyboard(
        template_id, inbounds, template_inbound_ids, selected_inbound_ids, server.name
    )

    await callback.message.edit_reply_markup(reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("template_bulk_add_selected_"))
async def add_selected_inbounds_to_template(callback: CallbackQuery, state: FSMContext):
    """Add selected inbounds to template."""
    parts = callback.data.split("_")
    template_id = int(parts[4])

    data = await state.get_data()
    selected_inbound_ids = data.get("selected_inbound_ids", set())
    data["template_id"] = template_id  # Ensure template_id is in state
    template_inbound_ids = data.get("template_inbound_ids", set())

    # Filter to only add inbounds not already in template
    inbounds_to_add = [
        inb_id for inb_id in selected_inbound_ids if inb_id not in template_inbound_ids
    ]

    if not inbounds_to_add:
        await callback.answer(
            t(
                "admin.templates.select_at_least_one_new_inbound",
                "❌ Выберите хотя бы один новый inbound.",
            ),
            show_alert=True,
        )
        return

    try:
        async with async_session_factory() as session:
            template_service = SubscriptionTemplateService(session)

            # Get current inbound IDs count for order
            template_inbound_ids = await get_template_inbounds(template_id)
            start_order = len(template_inbound_ids)

            # Add each selected inbound to template
            added_count = 0
            for order, inbound_id in enumerate(inbounds_to_add, start=start_order):
                try:
                    await template_service.add_inbound_to_template(
                        template_id=template_id,
                        inbound_id=inbound_id,
                        order=order,
                    )
                    added_count += 1
                except Exception as e:
                    logger.warning("Не удалось добавить inbound {} в шаблон: {}", inbound_id, e)

            await session.commit()

            # Запускаем фоновое обновление для привязанных подписок
            async def run_bg():
                try:
                    logger.debug("Фоновая задача bulk-add inbound запущена для шаблона {}", template_id)
                    async with async_session_factory() as bg_session:
                        bg_service = SubscriptionTemplateService(bg_session)
                        await bg_service._apply_template_inbounds_change(
                            template_id,
                            added_inbound_ids=set(inbounds_to_add),
                            removed_inbound_ids=set(),
                        )
                        await bg_session.commit()
                        logger.debug("Фоновая задача bulk-add inbound завершена")
                except Exception as e:
                    logger.warning("Фоновая задача bulk-add inbound завершилась с ошибкой: {}", e, exc_info=True)

            asyncio.create_task(run_bg())

            # Get updated template info
            template = await template_service.get_template(template_id)
            await template_service.get_template_inbounds(template_id)

            logger.info("Добавлено {} inbound к шаблону {}", added_count, template.name)

        # Show success message and return to server selection
        await state.clear()
        await state.update_data(template_id=template_id)
        await state.set_state(TemplateManagement.waiting_for_server_selection)

        from app.services.xui_service import XUIService

        async with async_session_factory() as session:
            xui_service = XUIService(session)
            servers = await xui_service.get_active_servers()

        await callback.message.edit_text(
            t(
                "admin.templates.added_inbounds_success_2",
                "✅ <b>Успешно добавлено {count} подключений</b>\n\n"
                "📝 Шаблон: <b>{name}</b>\n"
                "🔌 Всего подключений: {total_count}\n\n"
                "Выберите сервер для продолжения редактирования:",
                count=added_count,
                name=template.name,
                total_count=len(template.template_inbounds),
            ),
            reply_markup=get_servers_keyboard_for_template_edit(servers, template_id),
        )
        await callback.answer(
            t(
                "admin.templates.added_inbounds_alert",
                "✅ Добавлено {count} подключений",
                count=added_count,
            ),
            show_alert=True,
        )

    except XUIError as e:
        await state.clear()
        await callback.message.edit_text(
            t(
                "admin.templates.add_inbounds_error_xui",
                "❌ Ошибка при добавлении подключений: {error}",
                error=str(e),
            )
        )
        await callback.answer()
    except Exception as e:
        await state.clear()
        logger.error("Ошибка добавления inbound к шаблону: {}", e, exc_info=True)
        await callback.message.edit_text(
            t(
                "admin.templates.add_inbounds_error",
                "❌ Произошла ошибка при добавлении подключений",
            )
        )
        await callback.answer()


@router.callback_query(F.data.startswith("template_bulk_remove_selected_"))
async def remove_selected_inbounds_from_template(callback: CallbackQuery, state: FSMContext):
    """Remove selected inbounds from template."""
    parts = callback.data.split("_")
    template_id = int(parts[4])

    data = await state.get_data()
    selected_inbound_ids = data.get("selected_inbound_ids", set())
    data["template_id"] = template_id  # Ensure template_id is in state
    template_inbound_ids = data.get("template_inbound_ids", set())

    # Filter to only remove inbounds that are in template
    inbounds_to_remove = [
        inb_id for inb_id in selected_inbound_ids if inb_id in template_inbound_ids
    ]

    if not inbounds_to_remove:
        await callback.answer(
            t(
                "admin.templates.select_at_least_one_existing_inbound",
                "❌ Выберите хотя бы один существующий inbound.",
            ),
            show_alert=True,
        )
        return

    try:
        async with async_session_factory() as session:
            template_service = SubscriptionTemplateService(session)

            removed_count = 0
            for inbound_id in inbounds_to_remove:
                try:
                    await template_service.remove_inbound_from_template(template_id, inbound_id)
                    removed_count += 1
                except Exception as e:
                    logger.warning("Не удалось удалить inbound {} из шаблона: {}", inbound_id, e)

            await session.commit()

            # Запускаем фоновое обновление для привязанных подписок
            async def run_bg():
                try:
                    async with async_session_factory() as bg_session:
                        bg_service = SubscriptionTemplateService(bg_session)
                        await bg_service._apply_template_inbounds_change(
                            template_id,
                            added_inbound_ids=set(),
                            removed_inbound_ids=set(inbounds_to_remove),
                        )
                        await bg_session.commit()
                        logger.debug("Фоновая задача bulk-remove inbound завершена")
                except Exception as e:
                    logger.warning("Фоновая задача bulk-remove inbound завершилась с ошибкой: {}", e)

            asyncio.create_task(run_bg())

            # Get updated template info
            template = await template_service.get_template(template_id)
            await template_service.get_template_inbounds(template_id)

            logger.info("Удалено {} inbound из шаблона {}", removed_count, template.name)

        # Show success message and return to server selection
        await state.clear()
        await state.update_data(template_id=template_id)
        await state.set_state(TemplateManagement.waiting_for_server_selection)

        from app.services.xui_service import XUIService

        async with async_session_factory() as session:
            xui_service = XUIService(session)
            servers = await xui_service.get_active_servers()

        await callback.message.edit_text(
            t(
                "admin.templates.removed_inbounds_success",
                "✅ <b>Успешно удалено {count} подключений</b>\n\n"
                "📝 Шаблон: <b>{name}</b>\n"
                "🔌 Всего подключений: {total_count}\n\n"
                "Выберите сервер для продолжения редактирования:",
                count=removed_count,
                name=template.name,
                total_count=len(template.template_inbounds),
            ),
            reply_markup=get_servers_keyboard_for_template_edit(servers, template_id),
        )
        await callback.answer(
            t(
                "admin.templates.removed_inbounds_alert",
                "✅ Удалено {count} подключений",
                count=removed_count,
            ),
            show_alert=True,
        )

    except XUIError as e:
        await state.clear()
        await callback.message.edit_text(
            t(
                "admin.templates.remove_inbounds_error_xui",
                "❌ Ошибка при удалении подключений: {error}",
                error=str(e),
            )
        )
        await callback.answer()
    except Exception as e:
        await state.clear()
        logger.error("Ошибка удаления inbound из шаблона: {}", e, exc_info=True)
        await callback.message.edit_text(
            t(
                "admin.templates.remove_inbounds_error",
                "❌ Произошла ошибка при удалении подключений",
            )
        )
        await callback.answer()
