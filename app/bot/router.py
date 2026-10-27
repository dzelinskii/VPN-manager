"""Main bot router."""

from aiogram import Router

from app.bot.handlers import common, fallback, registration
from app.bot.handlers.admin import (
    broadcast,
    clients,
    dashboard,
    divergences,
    manual_clients,
    renewals,
    requests,
    servers,
    subscriptions,
    sync,
    templates,
)
from app.bot.handlers.user import subscriptions as user_subscriptions

_SUB_ROUTERS = [
    common.router,
    registration.router,
    dashboard.router,
    broadcast.router,
    servers.router,
    clients.router,
    subscriptions.router,
    sync.router,
    templates.router,
    requests.router,
    renewals.router,
    manual_clients.router,
    divergences.router,
    user_subscriptions.router,
    fallback.router,
]


def create_router() -> Router:
    """Собрать главный роутер из всех хэндлеров."""
    router = Router()
    for sub in _SUB_ROUTERS:
        router.include_router(sub)
    return router
