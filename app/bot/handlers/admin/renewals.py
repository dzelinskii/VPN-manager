"""Пульт продлений: кто истекает и продление в один тап."""

import html
from datetime import UTC, datetime

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.bot.filters import AdminFilter
from app.database import async_session_factory
from app.database.models import Subscription
from app.services.pricing import format_price, resolve_price
from app.services.renewal_service import (
    AlreadyRenewedError,
    RenewalService,
    SubscriptionNotFoundError,
)

router = Router()
router.message.filter(AdminFilter())
router.callback_query.filter(AdminFilter())

_PER_PAGE = 5
_DUE_WINDOW_DAYS = 7


def _as_utc(value: datetime) -> datetime:
    """Наивные даты из SQLite считаем UTC."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _format_expiry(expiry: datetime) -> str:
    days = (_as_utc(expiry) - datetime.now(UTC)).days
    if days < 0:
        return f"истекла {-days} дн. назад"
    return f"осталось {days} дн."


def _list_keyboard(items: list, page: int, total: int) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    for sub in items:
        ts = int(_as_utc(sub.expiry_date).timestamp())
        builder.button(
            text=f"➕ {sub.client.name} — {sub.name}",
            callback_data=f"renew:do:{sub.id}:{ts}",
        )
    builder.adjust(1)

    total_pages = max(1, -(-total // _PER_PAGE))
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"renew:page:{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"renew:page:{page + 1}"))
    if nav:
        builder.row(*nav)
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="admin_clients_menu"))
    return builder


async def _show_list(callback: CallbackQuery, page: int) -> None:
    async with async_session_factory() as session:
        due = await RenewalService(session).list_due(within_days=_DUE_WINDOW_DAYS)
        page_items = due[page * _PER_PAGE : (page + 1) * _PER_PAGE]
        lines = []
        for sub in page_items:
            lines.append(
                f"👤 <b>{html.escape(sub.client.name)}</b> — {html.escape(sub.name)}\n"
                f"   {_format_expiry(sub.expiry_date)}, {format_price(resolve_price(sub))}"
            )
        markup = _list_keyboard(page_items, page, len(due)).as_markup()

    if not due:
        await callback.message.edit_text(
            "✅ В ближайшие 7 дней продлевать некого.", reply_markup=markup
        )
        await callback.answer()
        return

    await callback.message.edit_text(
        f"💰 <b>К продлению: {len(due)}</b>\n\n" + "\n\n".join(lines),
        parse_mode="HTML",
        reply_markup=markup,
    )
    await callback.answer()


@router.callback_query(F.data == "admin_renewals")
async def show_renewals(callback: CallbackQuery) -> None:
    await _show_list(callback, page=0)


@router.callback_query(F.data.startswith("renew:page:"))
async def show_renewals_page(callback: CallbackQuery) -> None:
    await _show_list(callback, page=int(callback.data.split(":")[2]))


@router.callback_query(
    F.data.startswith("renew:do:") & ~F.data.startswith("renew:do:confirmed:")
)
async def renew_subscription(callback: CallbackQuery) -> None:
    """Продлить подписку, спросив подтверждение при незаданной цене."""
    _, _, sub_id, ts = callback.data.split(":")

    async with async_session_factory() as session:
        subscription = (
            await session.execute(
                select(Subscription)
                .where(Subscription.id == int(sub_id))
                .options(selectinload(Subscription.template))
            )
        ).scalar_one_or_none()
        if subscription is None:
            await callback.answer("Подписка не найдена", show_alert=True)
            await _show_list(callback, page=0)
            return
        price = resolve_price(subscription)
        name = subscription.name

    if price is None:
        builder = InlineKeyboardBuilder()
        builder.button(
            text="✅ Продлить без оплаты",
            callback_data=f"renew:do:confirmed:{sub_id}:{ts}",
        )
        builder.button(text="🔙 Отмена", callback_data="admin_renewals")
        builder.adjust(1)
        await callback.message.edit_text(
            f"⚠️ У подписки <b>{html.escape(name)}</b> не задана цена.\n\n"
            "Продление запишется как бесплатное. Если клиент платит — сначала "
            "задайте цену в подписке или в шаблоне.",
            parse_mode="HTML",
            reply_markup=builder.as_markup(),
        )
        await callback.answer()
        return

    await _do_renew(callback, int(sub_id), int(ts))


@router.callback_query(F.data.startswith("renew:do:confirmed:"))
async def confirm_renew_without_price(callback: CallbackQuery) -> None:
    """Продлить подписку без заданной цены после явного подтверждения."""
    _, _, _, sub_id, ts = callback.data.split(":")
    await _do_renew(callback, int(sub_id), int(ts))


async def _do_renew(callback: CallbackQuery, sub_id: int, ts: int) -> None:
    expected_expiry = datetime.fromtimestamp(ts, tz=UTC)

    async with async_session_factory() as session:
        try:
            result = await RenewalService(session).renew(int(sub_id), expected_expiry)
            await session.commit()
        except AlreadyRenewedError:
            await callback.answer("Уже продлено", show_alert=True)
            await _show_list(callback, page=0)
            return
        except SubscriptionNotFoundError:
            await callback.answer("Подписка не найдена", show_alert=True)
            await _show_list(callback, page=0)
            return
        except Exception as e:
            logger.error("Не удалось продлить подписку {}: {}", sub_id, e)
            await callback.answer("❌ Ошибка продления, см. логи", show_alert=True)
            return

    text = f"✅ Продлено на 30 дн., {format_price(result.amount_kopecks)}"
    if result.failed_connections:
        text += f"\n⚠️ Не применилось подключений: {result.failed_connections}"
    await callback.answer(text, show_alert=bool(result.failed_connections))
    await _show_list(callback, page=0)
