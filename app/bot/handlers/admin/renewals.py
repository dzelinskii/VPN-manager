"""Пульт продлений: кто истекает и продление в один тап."""

import html
from datetime import UTC, datetime

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from loguru import logger

from app.bot.filters import AdminFilter
from app.database import async_session_factory
from app.services.pricing import resolve_price
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


def _format_amount(amount: int | None) -> str:
    """Сумма для показа админу."""
    if amount is None:
        return "цена не задана"
    if amount == 0:
        return "бесплатно"
    return f"{amount / 100:.2f} ₽"


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
                f"   {_format_expiry(sub.expiry_date)}, {_format_amount(resolve_price(sub))}"
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


@router.callback_query(F.data.startswith("renew:do:"))
async def renew_subscription(callback: CallbackQuery) -> None:
    _, _, sub_id, ts = callback.data.split(":")
    expected_expiry = datetime.fromtimestamp(int(ts), tz=UTC)

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

    text = f"✅ Продлено на 30 дн., {_format_amount(result.amount_kopecks)}"
    if result.failed_connections:
        text += f"\n⚠️ Не применилось подключений: {result.failed_connections}"
    await callback.answer(text, show_alert=bool(result.failed_connections))
    await _show_list(callback, page=0)
