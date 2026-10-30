"""Хаб управления сервисами сервера + авто-обнаружение."""

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from loguru import logger

from app.bot.keyboards import get_back_keyboard
from app.database import async_session_factory
from app.utils.texts import t

router = Router()


@router.callback_query(F.data.startswith("server_services_"))
async def show_server_services(callback: CallbackQuery, state: FSMContext) -> None:
    """Show services installed on the server."""
    server_id = int(callback.data.split("_")[-1])

    async with async_session_factory() as session:
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        from app.database.models import Server

        result = await session.execute(
            select(Server)
            .options(
                selectinload(Server.xui_panel),
                selectinload(Server.awg_service),
                selectinload(Server.mtproxy_service),
            )
            .where(Server.id == server_id)
        )
        server = result.scalar_one_or_none()

    if not server:
        await callback.answer(
            t("admin.servers.errors.not_found", "❌ Сервер не найден."), show_alert=True
        )
        return

    text = t(
        "admin.servers.services.title",
        "⚙️ Управление сервисами сервера: <b>{name}</b>\n\n",
        name=server.name,
    )

    from aiogram.utils.keyboard import InlineKeyboardBuilder

    kb = InlineKeyboardBuilder()

    # 3x-ui
    if server.xui_panel:
        text += "✅ <b>3x-ui</b>: Установлен\n"
        kb.button(
            text=t("admin.servers.services.edit_xui", "✏️ 3x-ui"),
            callback_data=f"server_edit_xui_{server_id}",
        )
    else:
        text += "❌ <b>3x-ui</b>: Не установлен\n"
        kb.button(
            text=t("admin.servers.services.install_xui", "➕ Добавить 3x-ui"),
            callback_data=f"server_install_xui_{server_id}",
        )

    # AmneziaWG
    if server.awg_service:
        text += "✅ <b>AmneziaWG</b>: Установлен\n"
        kb.button(
            text=t("admin.servers.services.edit_awg", "✏️ AmneziaWG"),
            callback_data=f"server_edit_awg_{server_id}",
        )
    else:
        text += "❌ <b>AmneziaWG</b>: Не установлен\n"
        kb.button(
            text=t("admin.servers.services.install_awg", "➕ Добавить AmneziaWG"),
            callback_data=f"server_install_awg_{server_id}",
        )

    # MTProxy
    if server.mtproxy_service:
        text += "✅ <b>MTProxy</b>: Установлен\n"
        kb.button(
            text=t("admin.servers.services.edit_mtproxy", "✏️ MTProxy"),
            callback_data=f"server_edit_mtproxy_{server_id}",
        )
    else:
        text += "❌ <b>MTProxy</b>: Не установлен\n"
        kb.button(
            text=t("admin.servers.services.install_mtproxy", "➕ Добавить MTProxy"),
            callback_data=f"server_install_mtproxy_{server_id}",
        )

    text += "\nВы можете запустить автообнаружение сервисов через SSH."

    kb.button(
        text=t("admin.servers.services.autodiscover", "🔍 Автообнаружение сервисов"),
        callback_data=f"server_autodiscover_{server_id}",
    )
    kb.button(
        text=t("admin.servers.buttons.back", "🔙 Назад"), callback_data=f"server_select_{server_id}"
    )

    kb.adjust(1)

    await callback.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("server_autodiscover_"))
async def run_server_autodiscover(
    callback: CallbackQuery, state: FSMContext
) -> None:
    """Run AutoDiscoveryService over SSH."""
    server_id = int(callback.data.split("_")[-1])

    await callback.message.edit_text(
        t(
            "admin.servers.services.autodiscovering",
            "🔍 Запущено автообнаружение сервисов по SSH...\nПожалуйста, подождите.",
        ),
        reply_markup=None,
    )

    async with async_session_factory() as session:
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        from app.database.models import Server
        from app.database.models.inbound import AWGInbound, MTProxyInbound
        from app.database.models.services import AWGService, MTProxyService, XUIPanel

        result = await session.execute(
            select(Server)
            .options(
                selectinload(Server.xui_panel),
                selectinload(Server.awg_service),
                selectinload(Server.mtproxy_service),
                selectinload(Server.inbounds),
            )
            .where(Server.id == server_id)
        )
        server = result.scalar_one_or_none()

        if not server:
            await callback.answer(
                t("admin.servers.errors.not_found", "❌ Сервер не найден."), show_alert=True
            )
            return

        if not server.ssh_user:
            await callback.message.edit_text(
                t(
                    "admin.servers.services.ssh_not_configured",
                    "❌ SSH не настроен для этого сервера. Сначала настройте SSH.",
                ),
                reply_markup=get_back_keyboard(f"server_select_{server_id}"),
            )
            return

        from app.services.auto_discovery import AutoDiscoveryService

        discovery = AutoDiscoveryService(server)

        try:
            discovered = await discovery.discover_all()
        except Exception as e:
            logger.error("Ошибка автообнаружения на сервере {}: {}", server_id, e, exc_info=True)
            await callback.message.edit_text(
                t(
                    "admin.servers.services.discovery_error",
                    "❌ Ошибка при выполнении автообнаружения: {error}",
                    error=str(e),
                ),
                reply_markup=get_back_keyboard(f"server_services_{server_id}"),
            )
            return

        discovered_list = []
        if "3x-ui" in discovered:
            details = discovered["3x-ui"]
            if not server.xui_panel:
                panel = XUIPanel(
                    server_id=server.id,
                    url=f"https://{details['domain']}:{details['caddy_port']}",
                    username=details.get("username"),
                    password_encrypted="",
                    panel_path=details.get("web_path", "/"),
                    subscription_path=details.get("sub_path", "/sub/"),
                    subscription_json_path=details.get("sub_json_path", "/json/"),
                    caddy_port=details.get("caddy_port", 8443),
                )
                session.add(panel)
                discovered_list.append(
                    f"3x-ui ({details['domain']}:{details['caddy_port']})"
                )
            else:
                discovered_list.append("3x-ui (уже подключён)")

        if "amnezia-awg" in discovered:
            details = discovered["amnezia-awg"]
            container_name = details.get("container_name", "vpnbot-awg")
            is_external = details.get("is_external", False)
            if not server.awg_service:
                awg = AWGService(
                    server_id=server.id,
                    port=details.get("port", 51820),
                    subnet_ip=details.get("subnet_ip", "10.8.0.1"),
                    subnet_cidr=details.get("subnet_cidr", 24),
                    obfuscation=details.get("obfuscation", {}),
                    container_name=container_name,
                    is_external=is_external,
                    server_public_key=details.get("server_public_key") or None,
                    server_private_key=details.get("server_private_key") or None,
                )
                session.add(awg)
                origin = (
                    "контейнер приложения, переустановка запрещена"
                    if is_external else "контейнер бота"
                )
                discovered_list.append(
                    f"AmneziaWG {container_name} — {origin} (порт {details.get('port', 51820)})"
                )
            else:
                discovered_list.append("AmneziaWG (уже подключён)")

            if not any(ib.type == "awg_inbound" for ib in server.inbounds):
                awg_inbound = AWGInbound(
                    server_id=server.id,
                    protocol="awg",
                    remark="AmneziaWG",
                    port=details.get("port", 51820),
                )
                session.add(awg_inbound)

        if "mtproxy" in discovered:
            details = discovered["mtproxy"]
            if not server.mtproxy_service:
                mtproxy = MTProxyService(
                    server_id=server.id,
                    implementation=details.get("implementation", "mtg-multi"),
                    port=details.get("port", 443),
                    domain=details.get("domain"),
                    max_connections=details.get("max_connections"),
                    default_secret=details.get("secret"),
                )
                session.add(mtproxy)
                discovered_list.append(
                    f"MTProxy {details.get('implementation', 'mtg')} (порт {details.get('port', 443)})"
                )
            else:
                discovered_list.append("MTProxy (уже подключён)")

            if not any(ib.type == "mtproxy_inbound" for ib in server.inbounds):
                mtproxy_inbound = MTProxyInbound(
                    server_id=server.id,
                    protocol="mtproto",
                    remark="MTProxy",
                    port=details.get("port", 443),
                )
                session.add(mtproxy_inbound)

        if discovered_list:
            await session.commit()
            msg = t(
                "admin.servers.services.discovery_success",
                "✅ Автообнаружение завершено. Найдено:\n- {items}",
                items="\n- ".join(discovered_list),
            )
        else:
            msg = t(
                "admin.servers.services.discovery_empty",
                "❌ Никаких известных сервисов не найдено.",
            )

    from aiogram.utils.keyboard import InlineKeyboardBuilder

    kb = InlineKeyboardBuilder()
    kb.button(
        text=t("admin.servers.buttons.back", "🔙 Назад"),
        callback_data=f"server_services_{server_id}",
    )

    await callback.message.edit_text(msg, reply_markup=kb.as_markup())
    await callback.answer()
