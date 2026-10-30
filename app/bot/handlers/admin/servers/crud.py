"""CRUD серверов: список, добавление, выбор/детали, удаление, редактирование name/ip."""

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from aiogram.types import Message as TgMessage

from app.bot.handlers.admin.servers._shared import show_server_details
from app.bot.keyboards import (
    get_back_keyboard,
    get_confirm_keyboard,
    get_servers_keyboard,
)
from app.bot.states import ServerManagement
from app.database import async_session_factory
from app.services.xui_service import XUIService
from app.utils.texts import t

router = Router()


@router.callback_query(F.data == "admin_servers")
async def show_servers(callback: CallbackQuery, state: FSMContext) -> None:
    """Show servers list."""
    current_state = await state.get_state()
    if current_state:
        await state.clear()

    async with async_session_factory() as session:
        service = XUIService(session)
        servers = await service.get_all_servers()

    if not servers:
        await callback.message.edit_text(
            t(
                "admin.servers.list_empty",
                "📋 Список серверов пуст.\n\nНажмите '➕ Добавить сервер' для добавления первого сервера.",
            ),
            reply_markup=get_servers_keyboard([]),
        )
    else:
        await callback.message.edit_text(
            t("admin.servers.list", "📋 Список серверов:"),
            reply_markup=get_servers_keyboard(servers),
        )
    await callback.answer()


@router.callback_query(F.data == "server_add")
async def start_add_server(callback: CallbackQuery, state: FSMContext) -> None:
    """Start adding new server."""
    await state.clear()
    await state.set_state(ServerManagement.waiting_for_name)
    await callback.message.edit_text(
        t(
            "admin.servers.add_name",
            "➕ Добавление нового сервера\n\nВведите название сервера (например, 'NL-Server-1'):",
        ),
        reply_markup=get_back_keyboard("admin_servers"),
    )
    await callback.answer()


@router.message(ServerManagement.waiting_for_name)
async def process_server_name(message: TgMessage, state: FSMContext) -> None:
    """Process server name input."""
    name = message.text.strip()

    if not name:
        await message.answer(
            t("admin.servers.errors.empty_name", "❌ Название не может быть пустым."),
            reply_markup=get_back_keyboard("admin_servers"),
        )
        return

    if len(name) > 100:
        await message.answer(
            t(
                "admin.servers.errors.name_too_long",
                "❌ Название не должно превышать 100 символов.",
            ),
            reply_markup=get_back_keyboard("admin_servers"),
        )
        return

    await state.update_data(name=name)
    await state.set_state(ServerManagement.waiting_for_ip_address)
    await message.answer(
        t(
            "admin.servers.add_ip",
            "Введите IP-адрес сервера (например, 192.168.1.1):",
        ),
        reply_markup=get_back_keyboard("admin_servers"),
    )


@router.message(ServerManagement.waiting_for_ip_address)
async def process_server_ip_address(message: TgMessage, state: FSMContext) -> None:
    """Process server IP address input."""
    ip_address = message.text.strip()

    if not ip_address:
        await message.answer(
            t("admin.servers.errors.empty_ip", "❌ IP-адрес не может быть пустым."),
            reply_markup=get_back_keyboard("admin_servers"),
        )
        return

    await state.update_data(ip_address=ip_address)

    # Доступность здесь не проверяем: SSH-порт станет известен позже.
    # Настоящая проверка — test_connection при вводе SSH-кредов (порт из
    # настроек) и preflight перед установками (TCP-проба того же порта).
    await _create_and_finish_server_addition(message, state, ip_address)


async def _create_and_finish_server_addition(
    message: TgMessage, state: FSMContext, ip_address: str
) -> None:
    """Helper to create server and notify admin."""
    data = await state.get_data()
    name = data.get("name")

    async with async_session_factory() as session:
        service = XUIService(session)
        server = await service.create_server(
            name=name,
            ip_address=ip_address,
            url=None,
            username=None,
            password=None,
        )
        # is_online выяснит первый синк-цикл (TCP-проба SSH-порта)
        server.is_online = False
        await session.commit()

    await state.clear()

    text = t(
        "admin.servers.add_success",
        "✅ Сервер <b>{name}</b> успешно добавлен!\n\n🌐 IP: <code>{ip}</code>\n\nТеперь вы можете настроить SSH и установить необходимые сервисы.",
        name=name,
        ip=ip_address,
    )

    await message.edit_text(text, reply_markup=get_back_keyboard("admin_servers"))


@router.callback_query(F.data.startswith("server_select_"))
async def select_server(callback: CallbackQuery, state: FSMContext) -> None:
    """Show server details."""
    await state.clear()
    server_id = int(callback.data.split("_")[-1])

    async with async_session_factory() as session:
        service = XUIService(session)
        server = await service.get_server_by_id(server_id)

    if not server:
        await callback.answer(
            t("admin.servers.errors.not_found", "❌ Сервер не найден."), show_alert=True
        )
        return

    from app.services.server_monitor import ServerMonitor
    is_online = await ServerMonitor.check_server_status(server_id)

    status = (
        t("admin.servers.status.active", "✅ Активен")
        if server.is_active
        else t("admin.servers.status.inactive", "❌ Неактивен")
    )
    online_status = (
        t("admin.servers.status.online", "✅ В сети")
        if is_online
        else t("admin.servers.status.offline", "❌ Офлайн")
    )
    last_sync = (
        server.last_sync_at.strftime("%d.%m.%Y %H:%M")
        if hasattr(server, "last_sync_at") and server.last_sync_at
        else t("admin.servers.sync.never", "Никогда")
    )

    ip_info = f"\n🌐 IP: {server.ip_address}" if server.ip_address else ""

    text = t(
        "admin.servers.info_new",
        "🖥️ Сервер: {name}{ip_info}\n📊 Статус: {status}\n🌐 Доступность: {online_status}\n🔄 Последняя синхронизация: {last_sync}",
        name=server.name,
        ip_info=ip_info,
        status=status,
        online_status=online_status,
        last_sync=last_sync,
    )

    builder = []
    builder.append(
        {
            "text": t("admin.servers.buttons.edit", "✏️ Редактировать"),
            "callback_data": f"server_edit_main_{server_id}",
        }
    )
    builder.append(
        {
            "text": t("admin.servers.buttons.ssh_setup", "🔑 Настроить SSH"),
            "callback_data": f"server_setup_ssh_{server_id}",
        }
    )
    builder.append(
        {
            "text": t("admin.servers.buttons.manage_services", "⚙️ Управление сервисами"),
            "callback_data": f"server_services_{server_id}",
        }
    )
    builder.append(
        {
            "text": t("admin.servers.buttons.inbounds", "📊 Inbounds"),
            "callback_data": f"server_inbounds_{server_id}",
        }
    )
    builder.append(
        {
            "text": t("admin.servers.buttons.sync", "🔄 Синхронизировать"),
            "callback_data": f"server_sync_{server_id}",
        }
    )
    builder.append(
        {
            "text": t("admin.servers.buttons.test_connection", "🔌 Проверить подключение"),
            "callback_data": f"server_test_{server_id}",
        }
    )
    builder.append(
        {
            "text": t("admin.servers.buttons.delete", "🗑️ Удалить"),
            "callback_data": f"server_delete_{server_id}",
        }
    )
    builder.append(
        {"text": t("admin.servers.buttons.back", "🔙 Назад"), "callback_data": "admin_servers"}
    )

    from aiogram.utils.keyboard import InlineKeyboardBuilder

    kb = InlineKeyboardBuilder()
    for btn in builder:
        kb.button(**btn)
    kb.adjust(1)

    await callback.message.edit_text(text, reply_markup=kb.as_markup())
    await callback.answer()


@router.callback_query(F.data.startswith("server_delete_"))
async def confirm_delete_server(callback: CallbackQuery, state: FSMContext) -> None:
    """Confirm server deletion."""
    server_id = int(callback.data.split("_")[-1])
    await state.update_data(server_id=server_id)
    await state.set_state(ServerManagement.confirm_delete)

    await callback.message.edit_text(
        t(
            "admin.servers.delete_confirm",
            "⚠️ Вы уверены, что хотите удалить этот сервер?\n\nВсе связанные подписки будут также удалены!",
        ),
        reply_markup=get_confirm_keyboard(f"server_delete_{server_id}", "admin_servers"),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("confirm_server_delete_"))
async def delete_server(callback: CallbackQuery, state: FSMContext) -> None:
    """Delete server."""
    server_id = int(callback.data.split("_")[-1])

    async with async_session_factory() as session:
        service = XUIService(session)
        await service.delete_server(server_id)
        await session.commit()

    await state.clear()
    await callback.answer(t("admin.servers.deleted", "✅ Сервер удален."))
    await show_servers(callback, state)


@router.callback_query(F.data.startswith("server_edit_main_"))
async def edit_server(callback: CallbackQuery, state: FSMContext) -> None:
    """Show server edit menu."""
    server_id = int(callback.data.split("_")[-1])
    await state.update_data(server_id=server_id)

    async with async_session_factory() as session:
        service = XUIService(session)
        server = await service.get_server_by_id(server_id)

    if not server:
        await callback.answer(
            t("admin.servers.errors.not_found", "❌ Сервер не найден."), show_alert=True
        )
        return

    builder = []
    builder.append(
        {
            "text": t("admin.servers.buttons.edit_name", "✏️ Название"),
            "callback_data": "edit_server_name",
        }
    )
    builder.append(
        {
            "text": t("admin.servers.buttons.edit_ip", "🌐 IP-адрес"),
            "callback_data": "edit_server_ip",
        }
    )
    builder.append(
        {
            "text": t("admin.servers.buttons.back", "🔙 Назад"),
            "callback_data": f"server_select_{server_id}",
        }
    )

    from aiogram.utils.keyboard import InlineKeyboardBuilder

    kb = InlineKeyboardBuilder()
    for btn in builder:
        kb.button(**btn)
    kb.adjust(1)

    text = t(
        "admin.servers.edit_menu_new",
        "✏️ Редактирование сервера: <b>{name}</b>\n\n🌐 IP-адрес: {ip}\n\nВыберите поле для редактирования:",
        name=server.name,
        ip=server.ip_address,
    )

    await callback.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "edit_server_name")
async def start_edit_name(callback: CallbackQuery, state: FSMContext) -> None:
    """Start editing server name."""
    data = await state.get_data()
    server_id = data["server_id"]

    async with async_session_factory() as session:
        service = XUIService(session)
        server = await service.get_server_by_id(server_id)

    if not server:
        await callback.answer(
            t("admin.servers.errors.not_found", "❌ Сервер не найден."), show_alert=True
        )
        return

    await state.set_state(ServerManagement.waiting_for_edit_name)
    await callback.message.edit_text(
        t(
            "admin.servers.edit_name",
            "✏️ Редактирование названия сервера\n\nТекущее название: <b>{name}</b>\n\nВведите новое название (или /skip чтобы оставить текущее):",
            name=server.name,
        ),
        reply_markup=get_back_keyboard(f"server_select_{server_id}"),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(ServerManagement.waiting_for_edit_name)
async def process_edit_name(message: TgMessage, state: FSMContext) -> None:
    """Process server name edit."""
    data = await state.get_data()
    server_id = data["server_id"]
    new_name = message.text.strip()

    if new_name == "/skip":
        await show_server_details(message, state, server_id)
        return

    if not new_name:
        await message.answer(
            t("admin.servers.errors.empty_name", "❌ Название не может быть пустым.")
        )
        return

    if len(new_name) > 100:
        await message.answer(
            t("admin.servers.errors.name_too_long", "❌ Название не должно превышать 100 символов.")
        )
        return

    async with async_session_factory() as session:
        service = XUIService(session)
        server = await service.update_server(server_id, name=new_name)
        if server:
            await session.commit()
            await message.answer(
                t("admin.servers.name_changed", "✅ Название изменено на: {name}", name=new_name)
            )
            await edit_server_menu(message, state, server_id)


@router.callback_query(F.data == "edit_server_ip")
async def start_edit_ip_address(callback: CallbackQuery, state: FSMContext) -> None:
    """Start editing server IP address."""
    data = await state.get_data()
    server_id = data["server_id"]

    async with async_session_factory() as session:
        service = XUIService(session)
        server = await service.get_server_by_id(server_id)

    if not server:
        await callback.answer(
            t("admin.servers.errors.not_found", "❌ Сервер не найден."), show_alert=True
        )
        return

    await state.set_state(ServerManagement.waiting_for_edit_ip_address)
    await callback.message.edit_text(
        t(
            "admin.servers.edit_ip",
            "✏️ Редактирование IP-адреса сервера\n\nТекущий IP: <b>{ip}</b>\n\nВведите новый IP-адрес (или /skip чтобы оставить текущий):",
            ip=server.ip_address or "Не указан",
        ),
        reply_markup=get_back_keyboard(f"server_select_{server_id}"),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(ServerManagement.waiting_for_edit_ip_address)
async def process_edit_ip_address(message: TgMessage, state: FSMContext) -> None:
    """Process server IP edit."""
    data = await state.get_data()
    server_id = data["server_id"]
    new_ip = message.text.strip()

    if new_ip == "/skip":
        await show_server_details(message, state, server_id)
        return

    if not new_ip:
        await message.answer(
            t("admin.servers.errors.empty_ip", "❌ IP-адрес не может быть пустым.")
        )
        return

    async with async_session_factory() as session:
        service = XUIService(session)
        server = await service.update_server(server_id, ip_address=new_ip)
        if server:
            await session.commit()
            await message.answer(
                t("admin.servers.ip_changed", "✅ IP-адрес изменен на: {ip}", ip=new_ip)
            )

    await edit_server_menu(message, state, server_id)


async def edit_server_menu(message: TgMessage, state: FSMContext, server_id: int) -> None:
    """Return to server edit menu."""
    data = await state.get_data()
    data["server_id"] = server_id
    await state.update_data(data)

    async with async_session_factory() as session:
        service = XUIService(session)
        server = await service.get_server_by_id(server_id)

    if not server:
        await message.answer(t("admin.servers.errors.not_found", "❌ Сервер не найден."))
        return

    builder = []
    builder.append(
        {
            "text": t("admin.servers.buttons.edit_name", "✏️ Название"),
            "callback_data": "edit_server_name",
        }
    )
    builder.append(
        {
            "text": t("admin.servers.buttons.edit_ip", "🌐 IP-адрес"),
            "callback_data": "edit_server_ip",
        }
    )
    builder.append(
        {
            "text": t("admin.servers.buttons.back", "🔙 Назад"),
            "callback_data": f"server_select_{server_id}",
        }
    )

    from aiogram.utils.keyboard import InlineKeyboardBuilder

    kb = InlineKeyboardBuilder()
    for btn in builder:
        kb.button(**btn)
    kb.adjust(1)

    text = t(
        "admin.servers.edit_menu_new",
        "✏️ Редактирование сервера: <b>{name}</b>\n\n🌐 IP-адрес: {ip}\n\nВыберите поле для редактирования:",
        name=server.name,
        ip=server.ip_address,
    )

    await message.answer(text, reply_markup=kb.as_markup(), parse_mode="HTML")
