"""Цена подписки: разрешение приоритета, разбор ввода и запись.

Деньги хранятся целыми копейками: двоичный float не представляет десятичные
дроби точно и накапливает ошибку на суммировании, а SQLite не имеет типа
DECIMAL. Платёжные API тоже работают в минорных единицах.
"""

from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Subscription, SubscriptionTemplate


def resolve_price(subscription: Subscription) -> int | None:
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


MAX_PRICE_KOPECKS = 100_000_000  # 1 000 000 ₽ — заведомо выше любой разумной цены


def parse_price_kopecks(text: str) -> int:
    """Разобрать введённую админом цену в рублях и вернуть копейки.

    Принимает ``349.90``, ``349,90`` и ``350``. Ноль допустим — это осознанно
    бесплатная подписка.

    Raises:
        ValueError: на любой некорректный ввод. Тип важен: вызывающие ловят
            только его, а текст ошибки уходит в HTML-сообщение, поэтому сырой
            ввод в него не подставляется.
    """
    cleaned = text.strip().replace(",", ".").replace(" ", "")
    try:
        rubles = Decimal(cleaned)
    except InvalidOperation as e:
        raise ValueError("Не похоже на число") from e
    except ArithmeticError as e:
        # Экспоненты за пределами Emax дают decimal.Overflow — это тоже
        # ArithmeticError, а вызывающие ловят только ValueError.
        raise ValueError("Слишком большое число") from e

    # Decimal принимает inf и nan, а дальше они ломают и сравнение, и exponent.
    if not rubles.is_finite():
        raise ValueError("Не похоже на число")
    if rubles < 0:
        raise ValueError("Цена не может быть отрицательной")
    if -rubles.as_tuple().exponent > 2:
        raise ValueError("Копейка — минимальная единица, больше двух знаков после точки нельзя")

    try:
        kopecks = int(rubles * 100)
    except ArithmeticError as e:
        raise ValueError("Слишком большое число") from e

    if kopecks > MAX_PRICE_KOPECKS:
        raise ValueError(f"Слишком большая цена, максимум {MAX_PRICE_KOPECKS // 100} ₽")

    return kopecks


def format_price(amount: int | None) -> str:
    """Цена для показа админу."""
    if amount is None:
        return "цена не задана"
    if amount == 0:
        return "бесплатно"
    return f"{amount / 100:.2f} ₽"


async def set_template_price(
    session: AsyncSession, template_id: int, price_kopecks: int | None
) -> bool:
    """Задать или сбросить (``None``) цену шаблона.

    Отдельно от ``update_template``, потому что там ``None`` означает «не
    менять» и сбросить цену через неё невозможно.

    Returns:
        ``False``, если шаблон не найден.
    """
    template = (
        await session.execute(
            select(SubscriptionTemplate).where(SubscriptionTemplate.id == template_id)
        )
    ).scalar_one_or_none()
    if template is None:
        return False

    template.price_kopecks = price_kopecks
    await session.flush()
    return True


async def set_subscription_price(
    session: AsyncSession, subscription_id: int, price_kopecks: int | None
) -> bool:
    """Задать или сбросить (``None`` — брать из шаблона) цену подписки.

    Returns:
        ``False``, если подписка не найдена.
    """
    subscription = (
        await session.execute(
            select(Subscription).where(Subscription.id == subscription_id)
        )
    ).scalar_one_or_none()
    if subscription is None:
        return False

    subscription.price_kopecks = price_kopecks
    await session.flush()
    return True
