"""Inline keyboards for bot."""

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

from app.utils.texts import t


def get_user_keyboard() -> ReplyKeyboardMarkup:
    """Get persistent user keyboard with /start button.

    Returns:
        Reply keyboard markup that is always visible
    """
    builder = ReplyKeyboardBuilder()
    builder.button(text="/start")
    builder.adjust(1)
    return builder.as_markup(resize_keyboard=True)


def get_main_menu_keyboard(is_admin: bool, is_registered: bool = True) -> InlineKeyboardMarkup:
    """Get main menu keyboard.

    Args:
        is_admin: Whether client is admin
        is_registered: Whether client is registered (default: True)

    Returns:
        Inline keyboard markup
    """
    builder = InlineKeyboardBuilder()

    if is_registered:
        builder.button(
            text=t("keyboards.main_menu.my_subscriptions", "Мои подписки"),
            callback_data="my_subscriptions",
        )
        builder.button(
            text=t("keyboards.main_menu.all_sub_urls", "Все URL подписок"),
            callback_data="all_sub_urls",
        )
        builder.button(
            text=t("keyboards.main_menu.instruction", "📖 Инструкция"),
            callback_data="instruction_menu",
        )
        builder.button(
            text=t("keyboards.main_menu.request_subscription", "➕ Запросить подписку"),
            callback_data="request_subscription",
        )

        if is_admin:
            builder.button(
                text=t("keyboards.main_menu.admin_panel", "⚙️ Панель администратора"),
                callback_data="admin_menu",
            )
    else:
        builder.button(
            text=t("keyboards.main_menu.registration", "📝 Регистрация"),
            callback_data="start_registration",
        )

    builder.adjust(2)
    return builder.as_markup()


def get_admin_dashboard_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text=t("keyboards.admin.menu.clients", "👥 Клиентская часть"),
        callback_data="admin_clients_menu",
    )
    builder.button(
        text=t("keyboards.admin.menu.infra", "🖥 Инфраструктура"), callback_data="admin_infra_menu"
    )
    builder.button(
        text=t("keyboards.admin.menu.system", "🛠 Система и Настройки"),
        callback_data="admin_system_menu",
    )
    builder.button(
        text=t("keyboards.admin.menu.back_to_main", "🔙 В главное меню"), callback_data="main_menu"
    )
    builder.adjust(1)
    return builder.as_markup()


def get_admin_clients_menu_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text=t("keyboards.main_menu.admin.clients", "Управление клиентами"),
        callback_data="admin_clients",
    )
    builder.button(
        text=t("keyboards.main_menu.admin.templates", "📋 Шаблоны подписок"),
        callback_data="admin_templates",
    )
    builder.button(
        text=t("keyboards.main_menu.admin.broadcast", "📢 Уведомление всем"),
        callback_data="admin_broadcast",
    )
    builder.button(
        text=t("keyboards.main_menu.admin.renewals", "💰 Продления"),
        callback_data="admin_renewals",
    )
    builder.button(text=t("keyboards.common.back", "🔙 Назад"), callback_data="admin_menu")
    builder.adjust(1)
    return builder.as_markup()


def get_admin_infra_menu_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text=t("keyboards.main_menu.admin.servers", "Управление серверами"),
        callback_data="admin_servers",
    )
    builder.button(
        text=t("keyboards.main_menu.admin.sync", "🔄 Синхронизация"), callback_data="admin_sync"
    )
    builder.button(
        text=t("keyboards.unmanaged.menu", "🔍 Неуправляемые клиенты"),
        callback_data="unmanaged_menu",
    )
    builder.button(text=t("keyboards.common.back", "🔙 Назад"), callback_data="admin_menu")
    builder.adjust(1)
    return builder.as_markup()


def get_admin_system_menu_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text=t("keyboards.main_menu.admin.export", "Экспорт БД"), callback_data="admin_export"
    )
    builder.button(
        text=t("keyboards.main_menu.admin.reload_instructions", "🔄 Обновить инструкцию"),
        callback_data="admin_reload_instructions",
    )
    builder.button(
        text=t("keyboards.main_menu.admin.reload_texts", "🔄 Обновить тексты"),
        callback_data="admin_reload_texts",
    )
    builder.button(text=t("keyboards.common.back", "🔙 Назад"), callback_data="admin_menu")
    builder.adjust(2, 1, 1)  # First row has 2 buttons, next have 1
    return builder.as_markup()


def get_servers_keyboard(
    servers: list, action: str = "select", back_target: str = "admin_infra_menu"
) -> InlineKeyboardMarkup:
    """Get servers list keyboard.

    Args:
        servers: List of Server objects
        action: Action prefix for callback data
        back_target: Callback data for back button

    Returns:
        Inline keyboard markup
    """
    builder = InlineKeyboardBuilder()

    for server in servers:
        status = (
            t("keyboards.common.status.active", "Активен")
            if server.is_active
            else t("keyboards.common.status.inactive", "Неактивен")
        )
        panel_marker = "[Amnezia] " if getattr(server, "panel_type", "xui") == "amnezia" else ""
        builder.button(
            text=t(
                "keyboards.servers.server_button",
                "{status} {panel}{name}",
                status=status,
                panel=panel_marker,
                name=server.name,
            ),
            callback_data=f"server_{action}_{server.id}",
        )

    builder.button(text=t("keyboards.servers.add", "Добавить сервер"), callback_data="server_add")
    builder.button(text=t("keyboards.common.back", "Назад"), callback_data=back_target)
    builder.adjust(1)
    return builder.as_markup()


def get_server_panel_type_keyboard() -> InlineKeyboardMarkup:
    """Get keyboard for selecting server panel type."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text=t("keyboards.servers.type_xui", "🌐 3x-ui Panel"), callback_data="add_server_type_xui"
    )
    builder.button(
        text=t("keyboards.servers.type_amnezia", "🛡 Amnezia PHP Panel (Auto-Sync)"),
        callback_data="add_server_type_amnezia",
    )
    builder.button(text=t("keyboards.common.back", "🔙 Назад"), callback_data="admin_servers")
    builder.adjust(1)
    return builder.as_markup()


def get_servers_keyboard_for_template_edit(
    servers: list, template_id: int, action: str = "template_edit_server"
) -> InlineKeyboardMarkup:
    """Get servers keyboard for template editing.

    Args:
        servers: List of Server objects
        template_id: Template ID
        action: Action prefix for callback data

    Returns:
        Inline keyboard markup
    """
    builder = InlineKeyboardBuilder()

    for server in servers:
        status = (
            t("keyboards.common.status.active", "Активен")
            if server.is_active
            else t("keyboards.common.status.inactive", "Неактивен")
        )
        builder.button(
            text=t(
                "keyboards.servers.server_button",
                "{status} {name}",
                status=status,
                name=server.name,
            ),
            callback_data=f"server_{action}_{server.id}",
        )

    builder.button(
        text=t("keyboards.common.back", "Назад"), callback_data=f"template_select_{template_id}"
    )
    builder.adjust(1)
    return builder.as_markup()


def get_users_keyboard(users: list) -> InlineKeyboardMarkup:
    """Get users list keyboard.

    Args:
        users: List of User objects

    Returns:
        Inline keyboard markup
    """
    builder = InlineKeyboardBuilder()

    for user in users:
        status = (
            t("keyboards.common.status.active", "Активен")
            if user.is_active
            else t("keyboards.common.status.inactive", "Неактивен")
        )
        admin_badge = t("keyboards.users.admin_badge", "Админ") if user.is_admin else ""
        builder.button(
            text=t(
                "keyboards.users.user_button",
                "{status} {admin_badge} {name}",
                status=status,
                admin_badge=admin_badge,
                name=user.name,
            ),
            callback_data=f"user_select_{user.id}",
        )

    builder.button(text=t("keyboards.users.add", "Добавить пользователя"), callback_data="user_add")
    builder.button(text=t("keyboards.common.back", "Назад"), callback_data="admin_clients_menu")
    builder.adjust(1)
    return builder.as_markup()


def get_inbounds_keyboard(inbounds: list) -> InlineKeyboardMarkup:
    """Get inbounds list keyboard.

    Args:
        inbounds: List of Inbound objects

    Returns:
        Inline keyboard markup
    """
    builder = InlineKeyboardBuilder()

    for inbound in inbounds:
        status = (
            t("keyboards.common.status.active", "Активен")
            if inbound.is_active
            else t("keyboards.common.status.inactive", "Неактивен")
        )
        builder.button(
            text=t(
                "keyboards.inbounds.inbound_button",
                "{status} {remark} ({protocol})",
                status=status,
                remark=inbound.remark,
                protocol=inbound.protocol,
            ),
            callback_data=f"inbound_select_{inbound.id}",
        )

    builder.button(text=t("keyboards.common.back", "Назад"), callback_data="admin_infra_menu")
    builder.adjust(1)
    return builder.as_markup()


def get_confirm_keyboard(
    confirm_action: str,
    cancel_action: str = "cancel",
) -> InlineKeyboardMarkup:
    """Get confirmation keyboard.

    Args:
        confirm_action: Callback data for confirm button
        cancel_action: Callback data for cancel button

    Returns:
        Inline keyboard markup
    """
    builder = InlineKeyboardBuilder()
    builder.button(
        text=t("keyboards.common.confirm", "Подтвердить"), callback_data=f"confirm_{confirm_action}"
    )
    builder.button(text=t("keyboards.common.cancel", "Отмена"), callback_data=cancel_action)
    builder.adjust(2)
    return builder.as_markup()


def get_user_actions_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Get user actions keyboard.

    Args:
        user_id: User ID

    Returns:
        Inline keyboard markup
    """
    builder = InlineKeyboardBuilder()
    builder.button(
        text=t("keyboards.user_actions.rename", "Переименовать"),
        callback_data=f"user_rename_{user_id}",
    )
    builder.button(
        text=t("keyboards.user_actions.enable", "Включить"), callback_data=f"user_enable_{user_id}"
    )
    builder.button(
        text=t("keyboards.user_actions.disable", "Отключить"),
        callback_data=f"user_disable_{user_id}",
    )
    builder.button(
        text=t("keyboards.user_actions.delete", "Удалить"), callback_data=f"user_delete_{user_id}"
    )
    builder.button(text=t("keyboards.common.back", "Назад"), callback_data="admin_users")
    builder.adjust(2)
    return builder.as_markup()


def get_back_keyboard(callback_data: str = "admin_menu") -> InlineKeyboardMarkup:
    """Get back button keyboard.

    Args:
        callback_data: Callback data for back button

    Returns:
        Inline keyboard markup
    """
    builder = InlineKeyboardBuilder()
    builder.button(text=t("keyboards.common.back", "Назад"), callback_data=callback_data)
    return builder.as_markup()


def get_clients_keyboard(clients: list) -> InlineKeyboardMarkup:
    """Get clients list keyboard.

    Args:
        clients: List of Client objects

    Returns:
        Inline keyboard markup
    """
    builder = InlineKeyboardBuilder()

    for client in clients:
        status = (
            t("keyboards.common.status.active", "Активен")
            if client.is_active
            else t("keyboards.common.status.inactive", "Неактивен")
        )
        admin_badge = t("keyboards.users.admin_badge", "Админ") if client.is_admin else ""
        builder.button(
            text=t(
                "keyboards.clients.client_button",
                "{status} {admin_badge} {name}",
                status=status,
                admin_badge=admin_badge,
                name=client.name,
            ),
            callback_data=f"client_select_{client.id}",
        )

    builder.button(text=t("keyboards.clients.add", "Добавить клиента"), callback_data="client_add")
    builder.button(
        text=t("keyboards.clients.search", "Поиск клиентов"), callback_data="client_search"
    )
    builder.button(text=t("keyboards.common.back", "Назад"), callback_data="admin_clients_menu")
    builder.adjust(1)
    return builder.as_markup()


def get_clients_page_keyboard(
    clients: list,
    page: int,
    total_count: int,
    per_page: int = 5,
) -> InlineKeyboardMarkup:
    """Create inline keyboard for paginated clients list.

    Args:
        clients: List of clients for current page
        page: Current page number (0-indexed)
        total_count: Total number of active clients
        per_page: Number of clients per page

    Returns:
        Inline keyboard markup
    """
    total_pages = max(1, -(-total_count // per_page))  # Ceiling division
    current_page_display = page + 1  # 1-indexed for display

    builder = InlineKeyboardBuilder()

    for client in clients:
        status = "✅" if client.is_active else "❌"
        admin_badge = "🛡️" if client.is_admin else "👤"
        builder.button(
            text=t(
                "keyboards.clients.client_page_button",
                "{admin_badge} {status} {name} (ID: {id})",
                admin_badge=admin_badge,
                status=status,
                name=client.name,
                id=client.id,
            ),
            callback_data=f"client_select_{client.id}",
        )

    if total_pages > 1:
        pagination_row = []
        if page > 0:
            pagination_row.append(
                InlineKeyboardButton(
                    text=t(
                        "keyboards.pagination.prev",
                        "⬅️ Назад ({page}/{total_pages})",
                        page=page,
                        total_pages=total_pages,
                    ),
                    callback_data=f"clients_page_{page - 1}",
                )
            )
        pagination_row.append(
            InlineKeyboardButton(
                text=t(
                    "keyboards.pagination.current",
                    "📄 {current}/{total}",
                    current=current_page_display,
                    total=total_pages,
                ),
                callback_data="clients_page_current",
            )
        )
        if page < total_pages - 1:
            pagination_row.append(
                InlineKeyboardButton(
                    text=t(
                        "keyboards.pagination.next",
                        "Вперед ➡️ ({next_page}/{total_pages})",
                        next_page=page + 2,
                        total_pages=total_pages,
                    ),
                    callback_data=f"clients_page_{page + 1}",
                )
            )
        builder.row(*pagination_row)

    builder.button(
        text=t("keyboards.clients.search_icon", "🔍 Поиск клиентов"), callback_data="client_search"
    )
    builder.button(
        text=t("keyboards.clients.add_icon", "➕ Добавить клиента"), callback_data="client_add"
    )
    builder.button(text=t("keyboards.common.back_icon", "🔙 Назад"), callback_data="admin_clients")

    rows = [1] * len(clients)
    if total_pages > 1:
        rows.append(len(pagination_row))
    rows.extend([1, 1, 1])
    builder.adjust(*rows)

    return builder.as_markup()


def get_protocol_selection_keyboard() -> InlineKeyboardMarkup:
    """Get protocol selection keyboard.

    Returns:
        Inline keyboard markup
    """
    builder = InlineKeyboardBuilder()
    builder.button(text=t("keyboards.help.protocol_vless", "🛡 VLESS (Xray)"), callback_data="help_protocol_vless")
    builder.button(text=t("keyboards.help.protocol_awg", "🛡 AmneziaWG"), callback_data="instruction_full_awg")
    builder.button(text=t("keyboards.help.protocol_mtproxy", "🛡 MTProxy (Telegram)"), callback_data="instruction_full_mtproxy")
    builder.button(text=t("keyboards.help.faq", "❓ Частые вопросы (FAQ)"), callback_data="faq_main")
    builder.button(text=t("keyboards.common.main_menu", "🔙 Главное меню"), callback_data="main_menu")
    builder.adjust(1)
    return builder.as_markup()


def get_help_main_keyboard() -> InlineKeyboardMarkup:
    """Get VLESS OS selection keyboard.

    Returns:
        Inline keyboard markup
    """
    builder = InlineKeyboardBuilder()
    builder.button(text=t("keyboards.help.os_ios", "🍎 iOS / Mac"), callback_data="help_os_ios")
    builder.button(text=t("keyboards.help.os_android", "🤖 Android"), callback_data="help_os_android")
    builder.button(text=t("keyboards.help.os_windows", "🖥 Windows"), callback_data="help_os_windows")
    builder.button(text=t("keyboards.help.os_linux", "🐧 Linux"), callback_data="help_os_linux")
    builder.button(
        text=t("keyboards.common.back_icon", "🔙 Назад"), callback_data="instruction_menu"
    )
    builder.adjust(2, 2, 1)
    return builder.as_markup()


def get_instruction_menu_keyboard(os_name: str) -> InlineKeyboardMarkup:
    """Get instruction selection keyboard.

    Args:
        os_name: OS name (ios, android, windows, linux)

    Returns:
        Inline keyboard with instruction options
    """
    builder = InlineKeyboardBuilder()
    builder.button(
        text=t("keyboards.instruction.step_by_step", "📖 Пошаговая настройка"),
        callback_data=f"instruction_step_by_step_{os_name}",
    )
    builder.button(
        text=t("keyboards.instruction.full", "📄 Полная инструкция"),
        callback_data=f"instruction_full_{os_name}",
    )
    builder.button(
        text=t("keyboards.help.back_to_os", "🔙 Назад к выбору ОС"), callback_data="help_protocol_vless"
    )
    builder.adjust(1)
    return builder.as_markup()


def get_faq_list_keyboard(faq_list: list[dict]) -> InlineKeyboardMarkup:
    """Get FAQ list keyboard.

    Args:
        faq_list: List of FAQ dictionaries with 'question' keys

    Returns:
        Inline keyboard markup
    """
    builder = InlineKeyboardBuilder()
    for i, item in enumerate(faq_list):
        builder.button(text=item["question"], callback_data=f"faq_q_{i}")

    builder.button(text=t("keyboards.common.back_icon", "🔙 Назад"), callback_data="instruction_menu")
    builder.adjust(1)
    return builder.as_markup()


def get_faq_answer_keyboard() -> InlineKeyboardMarkup:
    """Get FAQ answer keyboard.

    Returns:
        Inline keyboard markup
    """
    builder = InlineKeyboardBuilder()
    builder.button(
        text=t("keyboards.faq.back_to_list", "🔙 К списку вопросов"), callback_data="faq_main"
    )
    builder.adjust(1)
    return builder.as_markup()


def get_step_navigation_keyboard(current_step: int, total_steps: int) -> InlineKeyboardMarkup:
    """Get step-by-step navigation keyboard.

    Args:
        current_step: Current step number (0-indexed)
        total_steps: Total number of steps

    Returns:
        Inline keyboard with navigation buttons
    """
    builder = InlineKeyboardBuilder()

    if current_step > 0:
        builder.button(
            text=t("keyboards.navigation.prev", "⬅️ Назад"), callback_data="instruction_prev"
        )
    builder.button(
        text=t(
            "keyboards.navigation.page",
            "📄 {current}/{total}",
            current=current_step + 1,
            total=total_steps,
        ),
        callback_data="instruction_page_current",
    )
    if current_step < total_steps - 1:
        builder.button(
            text=t("keyboards.navigation.next", "Далее ➡️"), callback_data="instruction_next"
        )
    else:
        builder.button(
            text=t("keyboards.navigation.done", "✅ Готово"), callback_data="instruction_done"
        )

    builder.button(
        text=t("keyboards.instruction.back_to_menu", "🔙 К меню инструкции"),
        callback_data="instruction_menu",
    )

    # Layout: navigation row + back button
    nav_buttons = 1  # page indicator always present
    if current_step > 0:
        nav_buttons += 1
    if current_step < total_steps - 1:
        nav_buttons += 1
    else:
        nav_buttons += 1  # "Готово" button

    builder.adjust(nav_buttons, 1)
    return builder.as_markup()


def get_client_search_keyboard() -> InlineKeyboardMarkup:
    """Get client search options keyboard.

    Returns:
        Inline keyboard markup with search field options
    """
    builder = InlineKeyboardBuilder()
    builder.button(
        text=t("keyboards.search.by_name", "👤 По имени"), callback_data="search_field_name"
    )
    builder.button(
        text=t("keyboards.search.by_email", "📧 По email"), callback_data="search_field_email"
    )
    builder.button(
        text=t("keyboards.search.by_telegram", "📱 По Telegram"),
        callback_data="search_field_telegram",
    )
    builder.button(
        text=t("keyboards.search.by_xui_email", "🔗 По XUI email"),
        callback_data="search_field_xui_email",
    )
    builder.button(
        text=t("keyboards.search.complex", "🔍 Комплексный поиск"), callback_data="search_field_all"
    )
    builder.button(text=t("keyboards.common.back", "Назад"), callback_data="admin_clients")
    builder.adjust(2)
    return builder.as_markup()


def get_registration_keyboard() -> InlineKeyboardMarkup:
    """Get registration method selection keyboard.

    Returns:
        Inline keyboard markup with registration options
    """
    builder = InlineKeyboardBuilder()
    builder.button(
        text=t("keyboards.registration.enter_name", "✏️ Ввести имя"), callback_data="reg_enter_name"
    )
    builder.button(
        text=t("keyboards.registration.use_telegram", "📱 Использовать Telegram"),
        callback_data="reg_use_telegram",
    )
    builder.button(text=t("keyboards.common.cancel_icon", "❌ Отмена"), callback_data="cancel")
    builder.adjust(2)
    return builder.as_markup()


def get_templates_keyboard(templates: list) -> InlineKeyboardMarkup:
    """Get templates list keyboard.

    Args:
        templates: List of SubscriptionTemplate objects

    Returns:
        Inline keyboard markup
    """
    builder = InlineKeyboardBuilder()

    for template in templates:
        status = (
            t("keyboards.common.status.active", "Активен")
            if template.is_active
            else t("keyboards.common.status.inactive", "Неактивен")
        )
        builder.button(
            text=t(
                "keyboards.templates.template_button",
                "{status} {name}",
                status=status,
                name=template.name,
            ),
            callback_data=f"template_select_{template.id}",
        )

    builder.button(
        text=t("keyboards.templates.add", "➕ Создать шаблон"), callback_data="template_add"
    )
    builder.button(text=t("keyboards.common.back", "Назад"), callback_data="admin_clients_menu")
    builder.adjust(1)
    return builder.as_markup()


def get_template_actions_keyboard(
    template_id: int, is_public: bool = False
) -> InlineKeyboardMarkup:
    """Get template actions keyboard.

    Args:
        template_id: Template ID
        is_public: Whether template is public

    Returns:
        Inline keyboard markup with buttons: Edit, Edit Inbounds, Toggle Public, Delete, Back
    """
    builder = InlineKeyboardBuilder()
    builder.button(
        text=t("keyboards.template_actions.edit", "✏️ Изменить"),
        callback_data=f"template_edit_menu_{template_id}",
    )
    builder.button(
        text=t("keyboards.template_actions.edit_inbounds", "✏️ Редактировать подключения"),
        callback_data=f"template_manage_inbounds_{template_id}",
    )
    if is_public:
        builder.button(
            text=t("keyboards.template_actions.hide_public", "🔒 Скрыть из доступа"),
            callback_data=f"admin_tpl_toggle_public_{template_id}",
        )
    else:
        builder.button(
            text=t("keyboards.template_actions.make_public", "👁 Сделать публичным"),
            callback_data=f"admin_tpl_toggle_public_{template_id}",
        )
    builder.button(
        text=t("keyboards.template_actions.delete", "❌ Удалить"),
        callback_data=f"template_delete_{template_id}",
    )
    builder.button(
        text=t("keyboards.common.back_icon", "🔙 Назад"), callback_data="admin_templates"
    )
    builder.adjust(2, 1, 2)
    return builder.as_markup()


def get_template_edit_menu_keyboard(template_id: int) -> InlineKeyboardMarkup:
    """Get template edit menu keyboard with field selection options.

    Args:
        template_id: Template ID

    Returns:
        Inline keyboard markup with edit field options
    """
    builder = InlineKeyboardBuilder()
    builder.button(
        text=t("keyboards.template_edit.name", "📝 Название"),
        callback_data=f"template_edit_name_{template_id}",
    )
    builder.button(
        text=t("keyboards.template_edit.description", "📝 Описание"),
        callback_data=f"template_edit_description_{template_id}",
    )
    builder.button(
        text=t("keyboards.template_edit.traffic", "📊 Трафик"),
        callback_data=f"template_edit_traffic_{template_id}",
    )
    builder.button(
        text=t("keyboards.template_edit.expiry", "📅 Срок действия"),
        callback_data=f"template_edit_expiry_{template_id}",
    )
    builder.button(
        text=t("keyboards.template_edit.notes", "📌 Заметки"),
        callback_data=f"template_edit_notes_{template_id}",
    )
    builder.button(
        text=t("keyboards.common.back_icon", "🔙 Назад"),
        callback_data=f"template_select_{template_id}",
    )
    builder.adjust(2)
    return builder.as_markup()


def get_template_inbounds_keyboard(
    template_id: int,
    template_inbounds: list,
) -> InlineKeyboardMarkup:
    """Get template inbounds management keyboard.

    Args:
        template_id: Template ID
        template_inbounds: List of current template inbounds

    Returns:
        Inline keyboard markup
    """
    builder = InlineKeyboardBuilder()

    # Current inbounds
    if template_inbounds:
        for ti in template_inbounds:
            status = "✅" if ti.inbound.is_active else "❌"
            builder.button(
                text=t(
                    "keyboards.template_inbounds.inbound_item",
                    "{status} {remark} ({server_name})",
                    status=status,
                    remark=ti.inbound.remark,
                    server_name=ti.inbound.server.name,
                ),
                callback_data=f"template_inbound_remove_{template_id}_{ti.inbound_id}",
            )
    else:
        builder.button(
            text=t("keyboards.template_inbounds.no_inbounds", "Нет подключений"),
            callback_data="template_no_inbounds",
        )

    # Action buttons
    builder.button(
        text=t("keyboards.template_inbounds.edit", "✏️ Редактировать подключения"),
        callback_data=f"template_edit_inbounds_{template_id}",
    )
    builder.button(
        text=t("keyboards.common.back", "Назад"), callback_data=f"template_select_{template_id}"
    )
    builder.adjust(1)
    return builder.as_markup()


def get_template_edit_inbounds_keyboard(
    template_id: int,
    all_inbounds: list,
    template_inbound_ids: set,
    selected_ids: set,
    server_name: str,
) -> InlineKeyboardMarkup:
    """Get template inbounds editing keyboard with multi-select.

    Args:
        template_id: Template ID
        all_inbounds: List of all available inbounds
        template_inbound_ids: Set of inbound IDs already in template
        selected_ids: Set of currently selected inbound IDs
        server_name: Name of the server (all inbounds are from this server)

    Returns:
        Inline keyboard markup
    """
    builder = InlineKeyboardBuilder()

    for inbound in all_inbounds:
        in_template_status = "✅" if inbound.id in template_inbound_ids else "❌"
        selected = "🔘" if inbound.id in selected_ids else "⭕"
        builder.button(
            text=t(
                "keyboards.template_edit_inbounds.item",
                "{selected} {remark} {status}",
                selected=selected,
                remark=inbound.remark,
                status=in_template_status,
            ),
            callback_data=f"template_inbound_edit_toggle_{template_id}_{inbound.id}",
        )

    builder.adjust(1)
    builder.button(
        text=t("keyboards.template_edit_inbounds.add_selected", "➕ Подключить выбранные"),
        callback_data=f"template_bulk_add_selected_{template_id}",
    )
    builder.button(
        text=t("keyboards.template_edit_inbounds.remove_selected", "❌ Отключить выбранные"),
        callback_data=f"template_bulk_remove_selected_{template_id}",
    )
    builder.button(
        text=t("keyboards.template_edit_inbounds.cancel", "🔙 Отмена"),
        callback_data=f"template_bulk_cancel_{template_id}",
    )
    builder.adjust(1)

    return builder.as_markup()


def get_inbound_selection_for_template(template_id: int, inbounds: list) -> InlineKeyboardMarkup:
    """Get inbound selection keyboard for template.

    Args:
        template_id: Template ID
        inbounds: List of available inbounds

    Returns:
        Inline keyboard markup
    """
    builder = InlineKeyboardBuilder()

    # Group inbounds by server
    from collections import defaultdict

    inbounds_by_server = defaultdict(list)
    for inbound in inbounds:
        inbounds_by_server[inbound.server.name].append(inbound)

    for server_name, server_inbounds in sorted(inbounds_by_server.items()):
        for inbound in server_inbounds:
            status = "✅" if inbound.is_active else "❌"
            builder.button(
                text=t(
                    "keyboards.template_inbound_select.item",
                    "📦 {status} {remark} ({server_name})",
                    status=status,
                    remark=inbound.remark,
                    server_name=server_name,
                ),
                callback_data=f"template_toggle_inbound_{template_id}_{inbound.id}",
            )

    builder.button(
        text=t("keyboards.template_inbound_select.add_selected", "➡️ Добавить выбранные"),
        callback_data="template_confirm_add_inbounds",
    )
    builder.button(
        text=t("keyboards.common.back", "Назад"), callback_data=f"template_select_{template_id}"
    )
    builder.adjust(1)
    return builder.as_markup()


def get_template_multi_select_keyboard(
    template_id: int, template_inbounds: list, selected_ids: set
) -> InlineKeyboardMarkup:
    """Get multi-select keyboard for template inbounds management.

    Args:
        template_id: Template ID
        template_inbounds: List of current template inbounds
        selected_ids: Set of selected inbound IDs

    Returns:
        Inline keyboard markup
    """
    builder = InlineKeyboardBuilder()

    for ti in template_inbounds:
        selected = "✅" if ti.inbound_id in selected_ids else "⭕"
        status = "🟢" if ti.inbound.is_active else "🔴"
        builder.button(
            text=t(
                "keyboards.template_multi_select.item",
                "{selected} {status} {remark} ({server_name})",
                selected=selected,
                status=status,
                remark=ti.inbound.remark,
                server_name=ti.inbound.server.name,
            ),
            callback_data=f"template_multi_select_{template_id}_{ti.inbound_id}",
        )

    builder.adjust(1)
    builder.button(
        text=t("keyboards.template_multi_select.delete", "🗑️ Удалить выбранные"),
        callback_data="template_multi_delete_inbounds",
    )
    builder.button(
        text=t("keyboards.common.exit", "🔙 Выход"), callback_data=f"template_select_{template_id}"
    )
    builder.adjust(1)

    return builder.as_markup()


def get_template_multi_select_confirm_keyboard() -> InlineKeyboardMarkup:
    """Get confirmation keyboard for template multi-select action."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text=t("keyboards.common.confirm_icon", "✅ Подтвердить"),
        callback_data="template_multi_confirm",
    )
    builder.button(
        text=t("keyboards.common.cancel_icon", "❌ Отмена"), callback_data="template_multi_cancel"
    )
    builder.adjust(1)
    return builder.as_markup()


def get_subscription_details_keyboard(
    subscription_id: int, is_active: bool, client_id: int, is_template: bool = False
) -> InlineKeyboardMarkup:
    """Get keyboard for subscription details.

    Args:
        subscription_id: Subscription ID
        is_active: Whether subscription is active
        client_id: Client ID
        is_template: Whether subscription is managed by a template

    Returns:
        Inline keyboard markup
    """
    builder = InlineKeyboardBuilder()

    builder.button(
        text=t("keyboards.subscription.inbounds", "📢 Inbounds"),
        callback_data=f"admin_sub_inbounds_{subscription_id}",
    )
    builder.button(
        text=t("keyboards.subscription.edit", "✏️ Редактировать"),
        callback_data=f"admin_sub_edit_{subscription_id}",
    )
    builder.button(
        text=t("keyboards.subscription.rebuild", "🔄 Переиспользовать токен"),
        callback_data=f"admin_sub_rebuild_{subscription_id}",
    )

    builder.button(
        text=t("keyboards.subscription.reset", "🔄 Сбросить подписку"),
        callback_data=f"sub_reset:{subscription_id}",
    )
    builder.button(
        text=t("keyboards.subscription.add_time", "⏳ Добавить время"),
        callback_data=f"sub_add_time:{subscription_id}",
    )

    if not is_template:
        builder.button(
            text=t("keyboards.subscription.edit_traffic", "⚙️ Изменить лимит трафика"),
            callback_data=f"sub_edit_traffic:{subscription_id}",
        )
        builder.button(
            text=t("keyboards.subscription.edit_expiry", "📅 Изменить дату окончания"),
            callback_data=f"sub_edit_expiry:{subscription_id}",
        )

    if is_active:
        builder.button(
            text=t("keyboards.subscription.disable", "❌ Отключить"),
            callback_data=f"admin_sub_disable_{subscription_id}",
        )
    else:
        builder.button(
            text=t("keyboards.subscription.enable", "✅ Включить"),
            callback_data=f"admin_sub_enable_{subscription_id}",
        )

    builder.button(
        text=t("keyboards.subscription.delete", "🗑️ Удалить"),
        callback_data=f"admin_sub_delete_{subscription_id}",
    )
    builder.button(
        text=t("keyboards.subscription.back_to_client", "🔙 Назад к клиенту"),
        callback_data=f"client_subscriptions_{client_id}",
    )

    builder.adjust(1)
    return builder.as_markup()


def get_subscription_rebuild_mode_keyboard(subscription_id: int) -> InlineKeyboardMarkup:
    """Get keyboard for selecting rebuild mode.

    Args:
        subscription_id: Subscription ID

    Returns:
        Inline keyboard markup
    """
    builder = InlineKeyboardBuilder()
    builder.button(
        text=t("keyboards.rebuild.template", "📋 Применить шаблон"),
        callback_data=f"rebuild_mode_template_{subscription_id}",
    )
    builder.button(
        text=t("keyboards.rebuild.manual", "⚙️ Настроить вручную"),
        callback_data=f"rebuild_mode_manual_{subscription_id}",
    )
    builder.button(
        text=t("keyboards.common.back", "🔙 Назад"),
        callback_data=f"admin_sub_detail_{subscription_id}",
    )
    builder.adjust(1)
    return builder.as_markup()


def get_public_templates_keyboard(templates: list) -> InlineKeyboardMarkup:
    """Get public templates list keyboard for users.

    Args:
        templates: List of public SubscriptionTemplate objects

    Returns:
        Inline keyboard markup
    """
    builder = InlineKeyboardBuilder()

    for template in templates:
        builder.button(
            text=t("keyboards.public_templates.item", "📦 {name}", name=template.name),
            callback_data=f"user_req_tpl_{template.id}",
        )

    builder.button(text=t("keyboards.common.back_icon", "🔙 Назад"), callback_data="back")
    builder.adjust(1)
    return builder.as_markup()


def get_cancel_keyboard() -> InlineKeyboardMarkup:
    """Get keyboard with a single cancel button.

    Returns:
        Inline keyboard markup
    """
    builder = InlineKeyboardBuilder()
    builder.button(text=t("keyboards.common.cancel_icon", "❌ Отмена"), callback_data="cancel")
    return builder.as_markup()


def get_unmanaged_list_keyboard(
    server_id: int, items: list, page: int = 0, per_page: int = 5
) -> InlineKeyboardMarkup:
    """Список неуправляемых клиентов сервера с действиями по каждому.

    На importable-клиенте — «Принять» + «Удалить»; иначе только «Удалить».
    """
    builder = InlineKeyboardBuilder()
    start = page * per_page
    page_items = items[start : start + per_page]
    for idx, it in enumerate(page_items, start=start):
        marker = "✅" if it.importable else "⚠️"
        builder.button(text=f"{marker} {it.email}", callback_data=f"uimp:noop:{server_id}:{idx}")
        if it.importable:
            builder.button(text="💾 Принять", callback_data=f"uimp:accept:{server_id}:{idx}")
        builder.button(text="🗑 Удалить", callback_data=f"uimp:del:{server_id}:{idx}")

    sizes = [3 if it.importable else 2 for it in page_items]
    builder.adjust(*sizes)

    total_pages = max(1, -(-len(items) // per_page))
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(text="⬅️", callback_data=f"uimp:page:{server_id}:{page - 1}")
        )
    if page < total_pages - 1:
        nav.append(
            InlineKeyboardButton(text="➡️", callback_data=f"uimp:page:{server_id}:{page + 1}")
        )
    if nav:
        builder.row(*nav)
    builder.row(
        InlineKeyboardButton(
            text=t("keyboards.common.back", "🔙 Назад"), callback_data="unmanaged_menu"
        )
    )
    return builder.as_markup()


def get_unmanaged_import_wizard_keyboard(server_id: int, idx: int) -> InlineKeyboardMarkup:
    """Мастер привязки при импорте: создать нового клиента или выбрать существующего."""
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Создать нового", callback_data=f"uimp:new:{server_id}:{idx}")
    builder.button(
        text="👤 Выбрать существующего", callback_data=f"uimp:existing:{server_id}:{idx}"
    )
    builder.button(
        text=t("keyboards.common.cancel", "🔙 Отмена"), callback_data=f"uimp:server:{server_id}"
    )
    builder.adjust(1)
    return builder.as_markup()


def get_full_delete_confirm_keyboard(subscription_id: int) -> InlineKeyboardMarkup:
    """Подтверждение удаления подписки при расхождении с панелью (ручные привязки).

    Три варианта: удалить клиента целиком / отвязать только известные БД привязки
    (сохранив ручные на панели) / отмена.
    """
    builder = InlineKeyboardBuilder()
    builder.button(
        text=t("keyboards.subdel.full", "🗑 Удалить целиком"),
        callback_data=f"confirm_admin_sub_delete_{subscription_id}",
    )
    builder.button(
        text=t("keyboards.subdel.known", "🔗 Отвязать только известное"),
        callback_data=f"sub_detach_known_{subscription_id}",
    )
    builder.button(
        text=t("keyboards.common.cancel", "❌ Отмена"),
        callback_data=f"admin_sub_detail_{subscription_id}",
    )
    builder.adjust(1)
    return builder.as_markup()


def get_divergence_digest_keyboard(batch_id: str, open_count: int) -> InlineKeyboardMarkup:
    """Клавиатура дайджеста расхождений: групповые действия + разбор по одному.

    Единая модель решения: применить деструктив / сохранить ручное / игнорировать.
    Групповые кнопки действуют поэлементно согласно kind каждого расхождения.
    """
    builder = InlineKeyboardBuilder()
    if open_count > 0:
        builder.button(
            text=t("keyboards.divergence.wizard", "🔎 Разобрать по одному"),
            callback_data=f"div:wiz:{batch_id}:0",
        )
        builder.button(
            text=t("keyboards.divergence.apply_all", "🗑 Применить ВСЁ"),
            callback_data=f"div:gall:apply:{batch_id}",
        )
        builder.button(
            text=t("keyboards.divergence.save_all", "💾 Сохранить ВСЁ"),
            callback_data=f"div:gall:save:{batch_id}",
        )
        builder.button(
            text=t("keyboards.divergence.ignore_all", "💤 Игнорировать всё"),
            callback_data=f"div:gall:ignore:{batch_id}",
        )
    builder.adjust(1)
    return builder.as_markup()


def get_divergence_item_keyboard(
    pid: int, batch_id: str, idx: int, total: int
) -> InlineKeyboardMarkup:
    """Клавиатура пошагового разбора одного расхождения: 🗑/💾/💤 + пагинация."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text=t("keyboards.divergence.apply", "🗑 Применить"),
        callback_data=f"div:item:apply:{pid}:{batch_id}:{idx}",
    )
    builder.button(
        text=t("keyboards.divergence.save", "💾 Сохранить"),
        callback_data=f"div:item:save:{pid}:{batch_id}:{idx}",
    )
    builder.button(
        text=t("keyboards.divergence.ignore", "💤 Игнорировать"),
        callback_data=f"div:item:ignore:{pid}:{batch_id}:{idx}",
    )
    builder.adjust(3)

    nav: list[InlineKeyboardButton] = []
    if idx > 0:
        nav.append(
            InlineKeyboardButton(text="◀", callback_data=f"div:wiz:{batch_id}:{idx - 1}")
        )
    if idx < total - 1:
        nav.append(
            InlineKeyboardButton(text="▶", callback_data=f"div:wiz:{batch_id}:{idx + 1}")
        )
    if nav:
        builder.row(*nav)
    return builder.as_markup()


def get_request_admin_keyboard(request_id: int) -> InlineKeyboardMarkup:
    """Get keyboard for admin to approve or reject a subscription request.

    Args:
        request_id: Request ID

    Returns:
        Inline keyboard markup
    """
    builder = InlineKeyboardBuilder()
    builder.button(
        text=t("keyboards.request_admin.approve", "✅ Одобрить"),
        callback_data=f"admin_req_approve_{request_id}",
    )
    builder.button(
        text=t("keyboards.request_admin.reject", "❌ Отклонить"),
        callback_data=f"admin_req_reject_{request_id}",
    )
    builder.adjust(2)
    return builder.as_markup()
