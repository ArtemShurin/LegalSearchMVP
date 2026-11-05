"""
Health-check — чтобы gateway (nginx) и оркестратор знали, жив ли backend и
готовы ли его зависимости, не слали трафик в мёртвый/не до конца готовый
контейнер. См. memory: project-backend-production-readiness, пункт 6.

Проверяет Postgres, Qdrant, Redis и загружены ли ML-модели в память — именно
то, без чего основные эндпоинты (поиск, загрузка документов) реально не
работают. Ollama намеренно НЕ входит в проверку "здоровья" — её отсутствие
не ломает приложение целиком, search_pipeline/routers/search.py уже
обрабатывает LLMUnavailableError отдельным деградированным ответом
(fallback_answer), а не падением; включать её сюда означало бы считать
backend "мёртвым" из-за внешнего сервиса, который сам по себе не часть
этого процесса и может временно отсутствовать без полного отказа сервиса.
"""

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from src.config import settings
from src.database import AsyncSessionLocal
from ml.models import models_loaded

router = APIRouter(tags=["health"])


async def _check_postgres() -> bool:
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


async def _check_qdrant() -> bool:
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            r = await client.get(f"{settings.qdrant_url}/collections")
            return r.status_code == 200
    except Exception:
        return False


async def _check_redis(request: Request) -> bool:
    try:
        pool = request.app.state.arq_pool
        return bool(await pool.ping())
    except Exception:
        return False


@router.get("/health")
async def health(request: Request):
    checks = {
        "postgres": await _check_postgres(),
        "qdrant": await _check_qdrant(),
        "redis": await _check_redis(request),
        "models_loaded": models_loaded(),
    }
    ok = all(checks.values())
    return JSONResponse(
        status_code=200 if ok else 503,
        content={"status": "ok" if ok else "degraded", "checks": checks},
    )
