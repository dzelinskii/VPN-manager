"""Пульт продлений: форматирование строк и регистрация экрана."""

from app.bot.handlers.admin import renewals


def test_router_is_registered():
    """Экран должен быть подключён к главному роутеру, иначе кнопка мертва."""
    from app.bot.router import _SUB_ROUTERS

    assert renewals.router in _SUB_ROUTERS


def test_menu_has_renewals_entry():
    from app.bot.keyboards.inline import get_admin_clients_menu_keyboard

    markup = get_admin_clients_menu_keyboard()
    callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert "admin_renewals" in callbacks
