"""Разрешение цены подписки."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.database.models import Subscription


def resolve_price(subscription: "Subscription") -> int | None:
    """Вернуть цену продления в копейках.

    Приоритет: переопределение на подписке, затем цена шаблона. ``None``
    означает, что цена не задана нигде; ``0`` — осознанно бесплатная подписка.

    Связь ``template`` должна быть загружена заранее: ленивая подгрузка в
    асинхронном контексте упадёт.
    """
    if subscription.price_kopecks is not None:
        return subscription.price_kopecks

    template = subscription.template
    if template is not None and template.price_kopecks is not None:
        return template.price_kopecks

    return None
