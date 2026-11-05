"""
Rate limiting на /search/ по компании (или пользователю, если компании нет).
Переиспользует Redis-подключение Arq (app.state.arq_pool — ArqRedis наследует
redis.asyncio.Redis, поддерживает incr/expire/ttl напрямую) — отдельного
Redis-клиента заводить не нужно.

Это ДРУГОЙ слой, не замена nginx'ового rate limiting (пункт 3, по IP): тот
не знает про company_id/auth, этот — именно про него, цель — не дать одной
активной компании забить очередь LLM (единственная GPU) для всех остальных.
"""

from fastapi import HTTPException

from src.config import settings
from src.services.metrics import search_requests_total


async def enforce_search_rate_limit(redis, company_id: str | None, user_id: str) -> None:
    """
    Простое фиксированное окно (не скользящее — проще, точности раз в
    минуту достаточно для этой цели). Ключ — по company_id, если он есть
    (большинство пользователей — сотрудники компаний), иначе по user_id.
    """
    scope = company_id or user_id
    key = f"rate_limit:search:{scope}"

    current = await redis.incr(key)
    if current == 1:
        await redis.expire(key, 60)

    if current > settings.rate_limit_search_per_minute:
        search_requests_total.labels(outcome="rate_limited").inc()
        ttl = await redis.ttl(key)
        retry_after = max(ttl, 1)
        raise HTTPException(
            status_code=429,
            detail=f"Слишком много запросов. Попробуйте снова через {retry_after}с.",
            headers={"Retry-After": str(retry_after)},
        )
