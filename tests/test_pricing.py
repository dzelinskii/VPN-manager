"""Разрешение цены подписки: переопределение важнее цены шаблона."""

from app.database.models import Subscription, SubscriptionTemplate
from app.services.pricing import resolve_price


def _template(price):
    return SubscriptionTemplate(name="t", default_total_gb=0, price_kopecks=price)


def _subscription(price, template):
    sub = Subscription(name="s", subscription_token="tok", total_gb=0, price_kopecks=price)
    sub.template = template
    return sub


def test_subscription_price_overrides_template():
    sub = _subscription(19900, _template(34990))
    assert resolve_price(sub) == 19900


def test_falls_back_to_template_price():
    sub = _subscription(None, _template(34990))
    assert resolve_price(sub) == 34990


def test_zero_is_free_not_absent():
    """0 — это осознанно бесплатно, а не «цена не задана»."""
    sub = _subscription(0, _template(34990))
    assert resolve_price(sub) == 0


def test_returns_none_when_nowhere_set():
    sub = _subscription(None, _template(None))
    assert resolve_price(sub) is None


def test_returns_none_without_template():
    sub = _subscription(None, None)
    assert resolve_price(sub) is None
