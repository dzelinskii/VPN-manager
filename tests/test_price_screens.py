"""Экраны задания цены: наличие кнопок и реальная маршрутизация callback'ов.

Проверки роутинга сделаны через `Router.propagate_event`, а не поиском подстрок
в исходнике: пересекающиеся префиксы callback_data ломаются молча, и тест по
тексту остаётся зелёным при сломанном роутинге.
"""

import pytest
from aiogram import Router
from aiogram.dispatcher.event.bases import UNHANDLED

from app.bot.keyboards.inline import (
    get_subscription_details_keyboard,
    get_template_edit_menu_keyboard,
)
from app.bot.states.admin import SubscriptionManagement, TemplateManagement


def _callbacks(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def _winning_handler(router: Router, data: str) -> str | None:
    """Имя обработчика, который реально победит для данного callback_data.

    Повторяет отбор aiogram: первый по порядку регистрации, чей фильтр прошёл.
    """
    for handler in router.callback_query.handlers:
        stub = type("Stub", (), {"data": data})()
        if all(_passes(f.callback, stub) for f in handler.filters or []):
            return handler.callback.__name__
    return None


def _passes(check, stub) -> bool:
    try:
        result = check(stub)
    except Exception:
        return False
    return bool(result) and result is not UNHANDLED


def test_template_edit_menu_has_price():
    assert "template_edit_price_7" in _callbacks(get_template_edit_menu_keyboard(7))


def test_subscription_details_has_price_button():
    markup = get_subscription_details_keyboard(
        subscription_id=5, is_active=True, client_id=1, is_template=True
    )
    assert "admin_sub_price_5" in _callbacks(markup)


def test_price_states_exist():
    assert TemplateManagement.editing_template_price is not None
    assert SubscriptionManagement.editing_subscription_price is not None


@pytest.mark.parametrize(
    ("callback_data", "expected"),
    [
        ("admin_sub_price_5", "start_edit_subscription_price"),
        ("admin_sub_price_reset_5", "reset_subscription_price"),
        ("admin_sub_price_cancel_5", "cancel_subscription_price"),
    ],
)
def test_subscription_price_callbacks_reach_their_handlers(callback_data, expected):
    """`reset_` и `cancel_` начинаются с `admin_sub_price_` и могли быть перехвачены."""
    from app.bot.handlers.admin import subscriptions

    assert _winning_handler(subscriptions.router, callback_data) == expected


@pytest.mark.parametrize(
    ("callback_data", "expected"),
    [
        ("renew:do:5:1757251200", "renew_subscription"),
        ("renew:do:confirmed:5:1757251200", "confirm_renew_without_price"),
    ],
)
def test_renewal_callbacks_reach_their_handlers(callback_data, expected):
    """`renew:do:confirmed:` вложен в `renew:do:` — без исключающего фильтра теряется."""
    from app.bot.handlers.admin import renewals

    assert _winning_handler(renewals.router, callback_data) == expected


def test_template_price_field_routes_to_its_state():
    from app.bot.handlers.admin import templates

    assert _winning_handler(templates.router, "template_edit_price_7") == "start_edit_template"
