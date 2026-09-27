"""Маршрутизация упакованных callback-ов.

Полный payload хранится в БД (nav_db), в callback_data уходит короткий ID.
Обработчики регистрируются префиксом через @route и вызываются диспетчером
как напрямую, так и из упакованных callback-ов.
"""
from typing import Awaitable, Callable, List, Tuple

ROUTES: List[Tuple[str, Callable]] = []


def route(prefix: str):
    """Зарегистрировать хендлер для payload с данным префиксом."""
    def deco(fn: Callable[..., Awaitable]):
        ROUTES.append((prefix, fn))
        return fn
    return deco


async def dispatch(call, payload: str) -> bool:
    """Вызвать хендлер по префиксу payload. False — если маршрут не найден."""
    for prefix, fn in ROUTES:
        if payload.startswith(prefix):
            await fn(call, payload)
            return True
    return False