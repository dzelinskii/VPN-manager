"""Домен AmneziaWG: установка/подключение, обфускация, desync-восстановление."""

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from aiogram.types import Message as TgMessage
from loguru import logger

from app.bot.keyboards import get_back_keyboard
from app.bot.states import AWGInstall
from app.database import async_session_factory
from app.utils.texts import t

router = Router()


@router.callback_query(F.data.startswith("server_edit_awg_"))
async def edit_awg_service(callback: CallbackQuery) -> None:
    """Edit AWG service or show desync menu."""
    server_id = int(callback.data.split("_")[-1])
    msg = await callback.message.edit_text("🔍 Проверка состояния AWG на сервере...")

    async with async_session_factory() as session:
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        from app.database.models import Server

        result = await session.execute(
            select(Server)
            .options(selectinload(Server.awg_service))
            .where(Server.id == server_id)
        )
        server = result.scalar_one_or_none()

    if not server or not server.awg_service:
        await msg.edit_text("❌ Сервер или сервис AWG не найдены.")
        return

    from app.services.installers.awg_installer import AWGInstaller
    from app.services.ssh_service import SSHManager

    try:
        ssh = SSHManager(server)
        installer = AWGInstaller(ssh)

        ok, err_msg = await installer.preflight_check()
        if not ok:
            from aiogram.utils.keyboard import InlineKeyboardBuilder
            kb = InlineKeyboardBuilder()
            kb.button(text="🔙 Назад", callback_data=f"server_services_{server_id}")
            await msg.edit_text(f"❌ Ошибка проверки прав SSH:\n<code>{err_msg}</code>", reply_markup=kb.as_markup(), parse_mode="HTML")
            return

        is_installed = await installer.check_already_installed()
    except Exception as e:
        from aiogram.utils.keyboard import InlineKeyboardBuilder
        kb = InlineKeyboardBuilder()
        kb.button(text="🔙 Назад", callback_data=f"server_services_{server_id}")
        await msg.edit_text(f"❌ Ошибка подключения по SSH:\n<code>{e}</code>", reply_markup=kb.as_markup(), parse_mode="HTML")
        return

    if not is_installed:
        from aiogram.utils.keyboard import InlineKeyboardBuilder
        kb = InlineKeyboardBuilder()
        kb.button(text="🛠 Восстановить сервис из БД", callback_data=f"awg_desync_restore_db_{server_id}")
        kb.button(text="🗑 Удалить из БД бота", callback_data=f"awg_desync_remove_db_{server_id}")
        kb.button(text="🔙 Назад", callback_data=f"server_services_{server_id}")
        kb.adjust(1)

        text = (
            "⚠️ <b>Обнаружен рассинхрон!</b>\n\n"
            "В БД бота числится сервис AmneziaWG, но физически на сервере он отсутствует.\n\n"
            "Выберите действие:"
        )
        await msg.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")
        return

    await msg.edit_text("Меню управления AWG (в разработке).", reply_markup=get_back_keyboard(f"server_services_{server_id}"))

@router.callback_query(F.data.startswith("server_install_awg_"))
async def start_awg_install(callback: CallbackQuery, state: FSMContext) -> None:
    """Start AWG installation: check SSH, then ask for port."""
    server_id = int(callback.data.split("_")[-1])

    async with async_session_factory() as session:
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        from app.database.models import Server

        result = await session.execute(
            select(Server)
            .options(selectinload(Server.awg_service))
            .where(Server.id == server_id)
        )
        server = result.scalar_one_or_none()

    if not server:
        await callback.answer(
            t("admin.servers.errors.not_found", "❌ Сервер не найден."), show_alert=True
        )
        return

    if server.awg_service:
        await callback.answer("AmneziaWG уже установлен на этом сервере.", show_alert=True)
        return

    if not server.ssh_user:
        await callback.message.edit_text(
            t(
                "admin.servers.services.ssh_not_configured",
                "❌ SSH не настроен для этого сервера. Сначала настройте SSH.",
            ),
            reply_markup=get_back_keyboard(f"server_services_{server_id}"),
        )
        return

    status_msg = await callback.message.edit_text(
        "🔄 Проверяю доступность сервера и SSH-подключение..."
    )

    from app.services.installers.awg_installer import AWGInstaller
    from app.services.ssh_service import SSHManager

    ssh = SSHManager(server)
    installer = AWGInstaller(ssh)
    ok, msg = await installer.preflight_check()

    if not ok:
        await status_msg.edit_text(
            f"❌ <b>Предварительная проверка не пройдена</b>\n\n{msg}",
            reply_markup=get_back_keyboard(f"server_services_{server_id}"),
            parse_mode="HTML",
        )
        return

    if await installer.check_already_installed():
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        await status_msg.edit_text(
            "🔍 <b>AmneziaWG уже установлен на этом сервере</b>\n\n"
            "Обнаружен контейнер <code>vpnbot-awg</code>.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="🔌 Подключить",
                    callback_data=f"awg_connect_existing_{server_id}",
                )],
                [InlineKeyboardButton(
                    text="🔄 Переустановить",
                    callback_data=f"awg_reinstall_{server_id}",
                )],
                [InlineKeyboardButton(
                    text="🔙 Назад",
                    callback_data=f"server_services_{server_id}",
                )],
            ]),
            parse_mode="HTML",
        )
        await callback.answer()
        return

    await state.update_data(server_id=server_id)

    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    await status_msg.edit_text(
        "🛡 <b>AmneziaWG не найден на сервере</b>\n\n"
        "Выберите способ добавления:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="📦 Установить новую",
                callback_data=f"awg_install_new_{server_id}",
            )],
            [InlineKeyboardButton(
                text="🔌 Подключить существующую",
                callback_data=f"awg_connect_existing_{server_id}",
            )],
            [InlineKeyboardButton(
                text="🔙 Назад",
                callback_data=f"server_services_{server_id}",
            )],
        ]),
        parse_mode="HTML",
    )
    await callback.answer()
    return


@router.callback_query(F.data.startswith("awg_install_new_"))
async def awg_install_new(callback: CallbackQuery, state: FSMContext) -> None:
    """Start fresh AWG installation."""
    server_id = int(callback.data.split("_")[-1])
    await state.update_data(server_id=server_id)

    from app.bot.handlers.admin.servers.first_setup import _check_first_setup
    policy_shown = await _check_first_setup(callback, state, server_id, "awg")
    if policy_shown:
        await callback.answer()
        return

    await _awg_ask_port(callback.message, state)
    await callback.answer()


@router.callback_query(F.data.startswith("awg_connect_existing_"))
async def awg_connect_existing(callback: CallbackQuery, state: FSMContext) -> None:
    """Connect existing AWG — auto-discover params and save to DB."""
    server_id = int(callback.data.split("_")[-1])

    msg = await callback.message.edit_text(
        "🔍 <b>Читаю конфигурацию AmneziaWG с сервера...</b>",
        parse_mode="HTML",
    )
    await callback.answer()

    try:
        from app.services.installers.awg_installer import AWGInstaller
        from app.services.ssh_service import SSHManager

        async with async_session_factory() as session:
            from sqlalchemy import select

            from app.database.models import Server

            server = (await session.execute(
                select(Server).where(Server.id == server_id)
            )).scalar_one()

        ssh = SSHManager(server)
        installer = AWGInstaller(ssh)

        ok, err_msg = await installer.preflight_check()
        if not ok:
            await msg.edit_text(f"❌ Ошибка проверки прав SSH:\n<code>{err_msg}</code>", reply_markup=get_back_keyboard(f"server_services_{server_id}"), parse_mode="HTML")
            return

        params = await installer.discover_existing()
    except Exception as e:
        logger.error("Ошибка авто-обнаружения AWG: {}", e, exc_info=True)
        await msg.edit_text(
            f"❌ <b>Не удалось прочитать конфигурацию</b>\n\n<code>{e}</code>",
            reply_markup=get_back_keyboard(f"server_services_{server_id}"),
            parse_mode="HTML",
        )
        return

    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    from aiogram.utils.text_decorations import html_decoration

    obf_text = "\n".join(
        f"  {k} = {html_decoration.quote(v)}"
        for k, v in params["obfuscation"].items()
    )
    await msg.edit_text(
        "🛡 <b>AmneziaWG найден!</b> Параметры из awg0.conf:\n\n"
        f"Порт: <code>{params['port']}/udp</code>\n"
        f"Подсеть: <code>{params['subnet_ip']}/{params['subnet_cidr']}</code>\n\n"
        f"<b>Обфускация:</b>\n{obf_text}\n\n"
        "Подключить с этими параметрами?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="✅ Подключить",
                callback_data="awg_confirm_connect",
            )],
            [InlineKeyboardButton(
                text="🔙 Отмена",
                callback_data=f"server_services_{server_id}",
            )],
        ]),
        parse_mode="HTML",
    )
    await state.update_data(
        server_id=server_id,
        connect_existing=True,
        port=params["port"],
        obfuscation=params["obfuscation"],
        subnet_ip=params["subnet_ip"],
        subnet_cidr=params["subnet_cidr"],
        server_public_key=params.get("server_public_key", ""),
        server_private_key=params.get("server_private_key", ""),
    )


@router.callback_query(F.data == "awg_confirm_connect")
async def awg_confirm_connect(callback: CallbackQuery, state: FSMContext) -> None:
    """Save discovered AWG params to DB."""
    await callback.answer()

    data = await state.get_data()
    server_id = data["server_id"]
    port = data["port"]
    obf = data["obfuscation"]
    subnet_ip = data.get("subnet_ip", "10.8.0.1")
    subnet_cidr = data.get("subnet_cidr", 24)
    await state.clear()

    try:
        async with async_session_factory() as session:
            from sqlalchemy import select

            from app.database.models import Server
            from app.database.models.inbound import AWGInbound
            from app.database.models.services import AWGService

            server = (await session.execute(
                select(Server).where(Server.id == server_id)
            )).scalar_one()

            existing_svc = await session.execute(
                select(AWGService).where(AWGService.server_id == server.id)
            )
            existing = existing_svc.scalar_one_or_none()
            if existing:
                existing.port = port
                existing.subnet_ip = subnet_ip
                existing.subnet_cidr = subnet_cidr
                existing.obfuscation = obf
                if data.get("server_public_key"):
                    existing.server_public_key = data["server_public_key"]
                if data.get("server_private_key"):
                    existing.server_private_key = data["server_private_key"]
            else:
                awg_service = AWGService(
                    server_id=server.id,
                    port=port,
                    subnet_ip=subnet_ip,
                    subnet_cidr=subnet_cidr,
                    obfuscation=obf,
                    server_public_key=data.get("server_public_key"),
                    server_private_key=data.get("server_private_key"),
                )
                session.add(awg_service)

            existing_ib = await session.execute(
                select(AWGInbound).where(AWGInbound.server_id == server.id)
            )
            if not existing_ib.scalar_one_or_none():
                awg_inbound = AWGInbound(
                    server_id=server.id,
                    protocol="awg",
                    remark=f"AmneziaWG:{port}",
                    port=port,
                )
                session.add(awg_inbound)
            await session.commit()

        await callback.message.edit_text(
            f"✅ <b>AmneziaWG подключён!</b>\n\n"
            f"Порт: <code>{port}/udp</code>\n"
            f"Контейнер: <code>vpnbot-awg</code>\n\n"
            "Сервис и inbound добавлены в базу данных.",
            reply_markup=get_back_keyboard(f"server_services_{server_id}"),
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error("Ошибка подключения AWG: {}", e, exc_info=True)
        await callback.message.edit_text(
            f"❌ <b>Ошибка подключения AmneziaWG</b>\n\n<code>{e}</code>",
            reply_markup=get_back_keyboard(f"server_services_{server_id}"),
            parse_mode="HTML",
        )


@router.callback_query(F.data.startswith("awg_reinstall_"))
async def awg_reinstall(callback: CallbackQuery, state: FSMContext) -> None:
    """Start AWG reinstallation flow (force=True)."""
    server_id = int(callback.data.split("_")[-1])
    await state.update_data(server_id=server_id, force_reinstall=True)
    await _awg_ask_port(callback.message, state)
    await callback.answer()


@router.callback_query(AWGInstall.waiting_for_port, F.data == "awg_port_random")
async def awg_port_random(callback: CallbackQuery, state: FSMContext) -> None:
    """Generate random port for AWG."""
    data = await state.get_data()
    server_id = data["server_id"]

    msg = await callback.message.edit_text("🔄 Подбираю свободный порт на сервере...")

    try:
        from app.services.ssh_service import SSHManager
        from app.services.vpn_providers.port_manager import PortManager

        async with async_session_factory() as session:
            from sqlalchemy import select

            from app.database.models import Server

            result = await session.execute(select(Server).where(Server.id == server_id))
            server = result.scalar_one()

        ssh = SSHManager(server)
        pm = PortManager(ssh)
        port = await pm.allocate_free_port()
    except Exception as e:
        logger.error("Ошибка выделения порта: {}", e)
        await msg.edit_text(
            f"❌ Не удалось подобрать порт: {e}",
            reply_markup=get_back_keyboard(f"server_services_{server_id}"),
        )
        await state.clear()
        return

    await state.update_data(port=port)
    await _show_awg_obfuscation_choice(msg, state, port)


@router.message(AWGInstall.waiting_for_port)
async def awg_port_manual(message: TgMessage, state: FSMContext) -> None:
    """Process manually entered AWG port."""
    text = message.text.strip()

    if not text.isdigit():
        await message.answer(
            "❌ Введите число (например, 30120).",
            reply_markup=get_back_keyboard("cancel"),
        )
        return

    port = int(text)
    if port < 1 or port > 65535:
        await message.answer("❌ Порт должен быть от 1 до 65535.")
        return

    data = await state.get_data()
    server_id = data["server_id"]

    msg = await message.answer(f"🔄 Проверяю порт {port} на сервере...")

    try:
        from app.services.ssh_service import SSHManager
        from app.services.vpn_providers.port_manager import PortManager

        async with async_session_factory() as session:
            from sqlalchemy import select

            from app.database.models import Server

            result = await session.execute(select(Server).where(Server.id == server_id))
            server = result.scalar_one()

        ssh = SSHManager(server)
        pm = PortManager(ssh)

        if not await pm.is_port_free(port):
            await msg.edit_text(
                f"❌ Порт {port} занят на сервере. Введите другой порт:",
                reply_markup=get_back_keyboard("cancel"),
            )
            return
    except Exception as e:
        logger.error("Ошибка проверки порта: {}", e)
        await msg.edit_text(
            f"❌ Не удалось проверить порт: {e}",
            reply_markup=get_back_keyboard(f"server_services_{server_id}"),
        )
        await state.clear()
        return

    await state.update_data(port=port)
    await _show_awg_obfuscation_choice(msg, state, port)


async def _show_awg_obfuscation_choice(message, state: FSMContext, port: int) -> None:
    """Show Quick/Advanced obfuscation mode selection."""
    await state.set_state(AWGInstall.waiting_for_obfuscation_mode)

    from aiogram.utils.keyboard import InlineKeyboardBuilder

    kb = InlineKeyboardBuilder()
    kb.button(text="⚡ Quick (авто)", callback_data="awg_obf_quick")
    kb.button(text="🔧 Advanced (вручную)", callback_data="awg_obf_advanced")
    kb.button(text="🔙 Отмена", callback_data="cancel")
    kb.adjust(1)

    await message.edit_text(
        f"🛡 <b>Установка AmneziaWG</b>\n\n"
        f"Порт: <code>{port}/udp</code>\n\n"
        f"Выберите режим настройки обфускации:\n"
        f"⚡ <b>Quick</b> — параметры генерируются автоматически\n"
        f"🔧 <b>Advanced</b> — задать Jc, Jmin, Jmax, S1-S4, H1-H4 вручную",
        reply_markup=kb.as_markup(),
        parse_mode="HTML",
    )


@router.callback_query(AWGInstall.waiting_for_obfuscation_mode, F.data == "awg_obf_quick")
async def awg_obf_quick(callback: CallbackQuery, state: FSMContext) -> None:
    """Quick mode: auto-generate obfuscation and show confirmation."""
    from app.services.installers.awg_installer import generate_obfuscation_params

    obf = generate_obfuscation_params()
    await state.update_data(obfuscation=obf)
    await _show_awg_confirm(callback.message, state)


@router.callback_query(AWGInstall.waiting_for_obfuscation_mode, F.data == "awg_obf_advanced")
async def awg_obf_advanced(callback: CallbackQuery, state: FSMContext) -> None:
    """Advanced mode: AWG 3.1 — администратор задаёт только Jc, остальное фикс."""
    await state.set_state(AWGInstall.waiting_for_obfuscation_params)

    await callback.message.edit_text(
        "🔧 <b>Advanced: параметр Jc (количество джанк-пакетов)</b>\n\n"
        "В формате 3.1 остальные параметры фиксированы:\n"
        "Jmin=10, Jmax=50, S1-S4=12, H1-H4=1..4, RandomTrailers=on, DisableCookies=on.\n\n"
        "Введите одно число Jc (рекомендуется 4-6, допустимо 3-10):",
        reply_markup=get_back_keyboard("cancel"),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AWGInstall.waiting_for_obfuscation_params)
async def awg_obf_params_input(message: TgMessage, state: FSMContext) -> None:
    """Parse manually entered Jc and build the fixed 3.1 parameter set."""
    from app.services.installers.awg_installer import generate_obfuscation_params

    text = message.text.strip()

    if not text.isdigit():
        await message.answer(
            "❌ Нужно одно число (например, 5). Попробуйте снова.",
            reply_markup=get_back_keyboard("cancel"),
        )
        return

    jc = int(text)
    if not 3 <= jc <= 10:
        await message.answer(
            "❌ Jc должен быть от 3 до 10. Попробуйте снова.",
            reply_markup=get_back_keyboard("cancel"),
        )
        return

    obf = generate_obfuscation_params()
    obf["Jc"] = str(jc)
    await state.update_data(obfuscation=obf)
    await _show_awg_confirm(message, state)


async def _show_awg_confirm(message, state: FSMContext) -> None:
    """Show installation confirmation with all parameters."""
    from aiogram.utils.text_decorations import html_decoration

    data = await state.get_data()
    port = data["port"]
    obf = data["obfuscation"]
    connect_existing = data.get("connect_existing", False)

    obf_text = "\n".join(
        f"  {k} = {html_decoration.quote(str(v))}" for k, v in obf.items()
    )

    await state.set_state(AWGInstall.confirm_install)

    action = "подключение" if connect_existing else "установку"
    btn_text = "✅ Подключить" if connect_existing else "✅ Установить"

    from aiogram.utils.keyboard import InlineKeyboardBuilder

    kb = InlineKeyboardBuilder()
    kb.button(text=btn_text, callback_data="awg_confirm_install")
    kb.button(text="❌ Отмена", callback_data="cancel")
    kb.adjust(1)

    target = message if isinstance(message, TgMessage) else message

    await target.edit_text(
        f"🛡 <b>Подтвердите {action} AmneziaWG</b>\n\n"
        f"Порт: <code>{port}/udp</code>\n\n"
        f"<b>Параметры обфускации:</b>\n{obf_text}\n\n"
        f"Нажмите «{'Подключить' if connect_existing else 'Установить'}» для начала.",
        reply_markup=kb.as_markup(),
        parse_mode="HTML",
    )


@router.callback_query(AWGInstall.confirm_install, F.data == "awg_confirm_install")
async def awg_execute_install(callback: CallbackQuery, state: FSMContext) -> None:
    """Execute AWG installation."""
    await callback.answer()

    data = await state.get_data()
    server_id = data["server_id"]
    port = data["port"]
    obf = data["obfuscation"]
    force = data.get("force_reinstall", False)
    connect_existing = data.get("connect_existing", False)

    await state.clear()

    if connect_existing:
        msg = await callback.message.edit_text(
            "🔌 <b>Подключение AmneziaWG...</b>\n\n"
            "Сохранение параметров в базу данных.",
            parse_mode="HTML",
        )
    else:
        msg = await callback.message.edit_text(
            "🔄 <b>Установка AmneziaWG...</b>\n\n"
            "Подготовка сервера, сборка контейнера, генерация ключей.\n"
            "Это может занять 1-2 минуты.",
            parse_mode="HTML",
        )

    try:
        async with async_session_factory() as session:
            from sqlalchemy import select
            from sqlalchemy.orm import selectinload

            from app.database.models import Server

            result = await session.execute(
                select(Server)
                .options(selectinload(Server.awg_service))
                .where(Server.id == server_id)
            )
            server = result.scalar_one()

            install_result = None
            if not connect_existing:
                from app.services.installers.awg_installer import AWGInstaller
                from app.services.ssh_service import SSHManager

                ssh = SSHManager(server, session=session)
                installer = AWGInstaller(
                    ssh,
                    progress_callback=lambda text: msg.edit_text(
                        f"🔄 <b>Установка AmneziaWG</b>\n\n{text}",
                        parse_mode="HTML",
                    ),
                )

                ok, err_msg = await installer.preflight_check()
                if not ok:
                    await msg.edit_text(f"❌ Ошибка проверки прав:\n<code>{err_msg}</code>", reply_markup=get_back_keyboard(f"server_services_{server_id}"), parse_mode="HTML")
                    return

                install_result = await installer.install(
                    port=port,
                    obfuscation=obf,
                    force=force,
                )

            from app.database.models.inbound import AWGInbound
            from app.database.models.services import AWGService

            existing_svc = await session.execute(
                select(AWGService).where(AWGService.server_id == server.id)
            )
            existing_awg = existing_svc.scalar_one_or_none()
            if existing_awg:
                existing_awg.port = port
                if install_result:
                    existing_awg.subnet_ip = install_result["subnet_ip"]
                    existing_awg.subnet_cidr = install_result["subnet_cidr"]
                    existing_awg.obfuscation = install_result["obfuscation"]
                    existing_awg.server_public_key = install_result.get("server_public_key")
                    existing_awg.server_private_key = install_result.get("server_private_key")
                else:
                    existing_awg.obfuscation = obf
            else:
                awg_service = AWGService(
                    server_id=server.id,
                    port=port,
                    subnet_ip=install_result["subnet_ip"] if install_result else "10.8.0.1",
                    subnet_cidr=install_result["subnet_cidr"] if install_result else 24,
                    obfuscation=install_result["obfuscation"] if install_result else obf,
                    server_public_key=install_result.get("server_public_key") if install_result else None,
                    server_private_key=install_result.get("server_private_key") if install_result else None,
                )
                session.add(awg_service)

            existing_ib = await session.execute(
                select(AWGInbound).where(AWGInbound.server_id == server.id)
            )
            if not existing_ib.scalar_one_or_none():
                awg_inbound = AWGInbound(
                    server_id=server.id,
                    protocol="awg",
                    remark=f"AmneziaWG:{port}",
                    port=port,
                )
                session.add(awg_inbound)
            await session.commit()

        action = "подключён" if connect_existing else ("переустановлен" if force else "установлен")
        await msg.edit_text(
            f"✅ <b>AmneziaWG {action}!</b>\n\n"
            f"Порт: <code>{port}/udp</code>\n"
            f"Контейнер: <code>vpnbot-awg</code>\n\n"
            "Сервис и inbound добавлены в базу данных.",
            reply_markup=get_back_keyboard(f"server_services_{server_id}"),
            parse_mode="HTML",
        )
    except Exception as e:
        from app.services.installers.base import AlreadyInstalledError

        if isinstance(e, AlreadyInstalledError):
            from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="🔄 Переустановить",
                    callback_data=f"awg_force_reinstall_{server_id}",
                )],
                [InlineKeyboardButton(
                    text="🔙 Назад",
                    callback_data=f"server_services_{server_id}",
                )],
            ])
            await msg.edit_text(
                f"⚠️ <b>AmneziaWG уже установлен</b>\n\n{e}",
                reply_markup=kb,
                parse_mode="HTML",
            )
        else:
            logger.error("Ошибка установки AWG: {}", e, exc_info=True)
            await msg.edit_text(
                f"❌ <b>Ошибка установки AmneziaWG</b>\n\n<code>{e}</code>",
                reply_markup=get_back_keyboard(f"server_services_{server_id}"),
                parse_mode="HTML",
            )


@router.callback_query(F.data.startswith("awg_force_reinstall_"))
async def awg_force_reinstall(callback: CallbackQuery, state: FSMContext) -> None:
    """Force reinstall AWG — remove existing and install fresh."""
    await callback.answer()

    server_id = int(callback.data.split("_")[-1])

    data = await state.get_data()
    port = data.get("port")
    obf = data.get("obfuscation")
    await state.clear()

    if not port:
        await callback.message.edit_text(
            "❌ Данные установки устарели. Начните установку заново.",
            reply_markup=get_back_keyboard(f"server_services_{server_id}"),
            parse_mode="HTML",
        )
        return

    msg = await callback.message.edit_text(
        "🔄 <b>Переустановка AmneziaWG...</b>\n\n"
        "Удаление старой установки и запуск новой.",
        parse_mode="HTML",
    )

    try:
        from app.services.installers.awg_installer import AWGInstaller
        from app.services.ssh_service import SSHManager

        async with async_session_factory() as session:
            from sqlalchemy import select
            from sqlalchemy.orm import selectinload

            from app.database.models import Server

            result = await session.execute(
                select(Server)
                .options(selectinload(Server.awg_service))
                .where(Server.id == server_id)
            )
            server = result.scalar_one()

            ssh = SSHManager(server, session=session)
            installer = AWGInstaller(
                ssh,
                progress_callback=lambda text: msg.edit_text(
                    f"🔄 <b>Переустановка AmneziaWG</b>\n\n{text}",
                    parse_mode="HTML",
                ),
            )

            install_result = await installer.install(
                port=port,
                obfuscation=obf,
                force=True,
            )

            from app.database.models.inbound import AWGInbound
            from app.database.models.services import AWGService

            existing_svc = await session.execute(
                select(AWGService).where(AWGService.server_id == server.id)
            )
            existing = existing_svc.scalar_one_or_none()
            if existing:
                existing.port = port
                existing.subnet_ip = install_result["subnet_ip"]
                existing.subnet_cidr = install_result["subnet_cidr"]
                existing.obfuscation = install_result["obfuscation"]
                existing.server_public_key = install_result.get("server_public_key")
                existing.server_private_key = install_result.get("server_private_key")
            else:
                awg_service = AWGService(
                    server_id=server.id,
                    port=port,
                    subnet_ip=install_result["subnet_ip"],
                    subnet_cidr=install_result["subnet_cidr"],
                    obfuscation=install_result["obfuscation"],
                    server_public_key=install_result.get("server_public_key"),
                    server_private_key=install_result.get("server_private_key"),
                )
                session.add(awg_service)

            existing_ib = await session.execute(
                select(AWGInbound).where(AWGInbound.server_id == server.id)
            )
            if not existing_ib.scalar_one_or_none():
                awg_inbound = AWGInbound(
                    server_id=server.id,
                    protocol="awg",
                    remark=f"AmneziaWG:{port}",
                    port=port,
                )
                session.add(awg_inbound)
            await session.commit()

        await msg.edit_text(
            "✅ <b>AmneziaWG переустановлен!</b>\n\n"
            f"Порт: <code>{port}/udp</code>\n"
            f"Контейнер: <code>vpnbot-awg</code>\n\n"
            "Сервис обновлён в базе данных.",
            reply_markup=get_back_keyboard(f"server_services_{server_id}"),
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error("Ошибка переустановки AWG: {}", e, exc_info=True)
        await msg.edit_text(
            f"❌ <b>Ошибка переустановки AmneziaWG</b>\n\n<code>{e}</code>",
            reply_markup=get_back_keyboard(f"server_services_{server_id}"),
            parse_mode="HTML",
        )


@router.callback_query(F.data.startswith("awg_desync_remove_db_"))
async def awg_desync_remove_db(callback: CallbackQuery, state: FSMContext) -> None:
    server_id = int(callback.data.split("_")[-1])
    async with async_session_factory() as session:
        from sqlalchemy import delete

        from app.database.models import AWGService, Inbound
        await session.execute(delete(AWGService).where(AWGService.server_id == server_id))
        await session.execute(delete(Inbound).where(Inbound.server_id == server_id, Inbound.protocol == "awg"))
        await session.commit()
    await callback.answer("✅ AWG удален из БД", show_alert=True)
    from app.bot.handlers.admin.servers.services import show_server_services
    await show_server_services(callback, state)

@router.callback_query(F.data.startswith("awg_desync_restore_db_"))
async def awg_desync_restore_db(callback: CallbackQuery, state: FSMContext) -> None:
    server_id = int(callback.data.split("_")[-1])

    msg = await callback.message.edit_text("🔄 <b>Восстановление AWG из БД...</b>\n\nНачинаю установку...", parse_mode="HTML")

    try:
        async with async_session_factory() as session:
            from sqlalchemy import select
            from sqlalchemy.orm import selectinload

            from app.database.models import AWGInboundConnection, Inbound, Server

            server = (await session.execute(select(Server).options(selectinload(Server.awg_service)).where(Server.id == server_id))).scalar_one()

            from app.services.installers.awg_installer import AWGInstaller
            from app.services.ssh_service import SSHManager

            installer = AWGInstaller(SSHManager(server, session=session), progress_callback=lambda text: msg.edit_text(f"🔄 <b>Восстановление AWG</b>\n\n{text}", parse_mode="HTML"))

            # Need to initialize sudo if needed
            ok, err_msg = await installer.preflight_check()
            if not ok:
                await msg.edit_text(f"❌ Ошибка проверки прав:\n<code>{err_msg}</code>", reply_markup=get_back_keyboard(f"server_services_{server_id}"), parse_mode="HTML")
                return


            await installer.install(
                port=server.awg_service.port,
                subnet_ip=server.awg_service.subnet_ip,
                subnet_cidr=server.awg_service.subnet_cidr,
                obfuscation=server.awg_service.obfuscation,
                force=True
            )

            # Now restore peers
            from app.services.vpn_providers.factory import get_vpn_provider

            awg_inbound = (await session.execute(select(Inbound).where(Inbound.server_id == server_id, Inbound.protocol == "awg"))).scalar_one_or_none()
            if awg_inbound:
                connections = (await session.execute(select(AWGInboundConnection).where(AWGInboundConnection.inbound_id == awg_inbound.id))).scalars().all()
                if connections:
                    provider = get_vpn_provider(server, inbound_type="awg_inbound")
                    for conn in connections:
                        if conn.is_enabled:
                            await provider.enable_client(awg_inbound, conn)
                    await provider.close()

        await msg.edit_text("✅ <b>AWG успешно восстановлен!</b>\n\nВсе конфигурации и ключи перенесены на сервер.", reply_markup=get_back_keyboard(f"server_services_{server_id}"), parse_mode="HTML")
    except Exception as e:
        logger.error("Ошибка восстановления AWG: {}", e, exc_info=True)
        await msg.edit_text(f"❌ Ошибка восстановления:\n<code>{e}</code>", reply_markup=get_back_keyboard(f"server_services_{server_id}"), parse_mode="HTML")


async def _awg_ask_port(message_or_callback, state: FSMContext) -> None:
    """Show AWG port input."""
    await state.set_state(AWGInstall.waiting_for_port)

    data = await state.get_data()
    server_id = data["server_id"]

    from aiogram.utils.keyboard import InlineKeyboardBuilder

    kb = InlineKeyboardBuilder()
    kb.button(text="🎲 Сгенерировать случайный", callback_data="awg_port_random")
    kb.button(text="🔙 Отмена", callback_data=f"server_services_{server_id}")
    kb.adjust(1)

    target = message_or_callback if isinstance(message_or_callback, TgMessage) else message_or_callback.message if hasattr(message_or_callback, "message") else message_or_callback
    await target.edit_text(
        "🛡 <b>Установка AmneziaWG</b>\n\n"
        "Введите UDP-порт для AWG (рекомендуется диапазон 30000-50000).\n"
        "Бот проверит, свободен ли порт на сервере.",
        reply_markup=kb.as_markup(),
        parse_mode="HTML",
    )
