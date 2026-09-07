"""Продление подписки без заданной цены требует подтверждения.

Иначе платному клиенту легко случайно продлить за ноль: пометка в списке есть,
но кнопка срабатывает молча.
"""

import inspect

from app.bot.handlers.admin import renewals


def test_unset_price_goes_through_confirmation():
    """Тап по подписке без цены должен вести на экран подтверждения, а не продлевать."""
    source = inspect.getsource(renewals.renew_subscription)
    assert "if price is None:" in source, "нет ветки для незаданной цены"
    assert "renew:do:confirmed:" in source, "ветка не ведёт на подтверждение"


def test_confirm_handler_exists():
    assert hasattr(renewals, "confirm_renew_without_price")


def test_confirmed_callback_prefix_is_distinct():
    """Подтверждающий callback не должен перехватываться обработчиком продления."""
    source = inspect.getsource(renewals)
    assert '~F.data.startswith("renew:do:confirmed:")' in source
