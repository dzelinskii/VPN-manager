"""Экраны задания цены: шаблон и подписка."""

from app.bot.keyboards.inline import get_template_edit_menu_keyboard
from app.bot.states.admin import SubscriptionManagement, TemplateManagement


def _callbacks(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def test_template_edit_menu_has_price():
    assert "template_edit_price_7" in _callbacks(get_template_edit_menu_keyboard(7))


def test_template_price_state_exists():
    assert TemplateManagement.editing_template_price is not None


def test_subscription_price_state_exists():
    assert SubscriptionManagement.editing_subscription_price is not None


def test_subscription_details_has_price_button():
    from app.bot.keyboards.inline import get_subscription_details_keyboard

    markup = get_subscription_details_keyboard(
        subscription_id=5, is_active=True, client_id=1, is_template=True
    )
    assert "admin_sub_price_5" in _callbacks(markup)


def test_price_reset_is_not_shadowed_by_price_screen():
    """`admin_sub_price_reset_N` начинается с `admin_sub_price_`.

    Без исключающего фильтра экран цены перехватил бы кнопку сброса, потому что
    зарегистрирован раньше.
    """
    import inspect

    from app.bot.handlers.admin import subscriptions

    source = inspect.getsource(subscriptions)
    assert '~F.data.startswith("admin_sub_price_reset_")' in source


def test_template_edit_field_maps_price_to_state():
    """Поле price должно быть в маршрутизации, иначе кнопка ведёт в «неизвестное поле»."""
    import inspect

    from app.bot.handlers.admin import templates

    source = inspect.getsource(templates.start_edit_template)
    assert '"price": TemplateManagement.editing_template_price' in source
