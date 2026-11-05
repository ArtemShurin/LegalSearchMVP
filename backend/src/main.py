import asyncio
import time
from contextlib import asynccontextmanager

import sentry_sdk
import torch
from arq import create_pool
from arq.connections import RedisSettings
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from src.config import settings
from src.routers import auth, search, history, collections, documents, health, telemetry
from src.services import activity_log
from ml.models import load_models

# Инициализируется, только если реально задан DSN (переменная окружения
# SENTRY_DSN) — пустая строка по умолчанию, значит отслеживание ошибок
# выключено, пока кто-то не заведёт аккаунт Sentry и не впишет настоящий
# DSN в backend/.env. Без этого условия sentry_sdk.init("") тихо ничего не
# делает тоже, но так явнее видно намерение в логах при старте.
if settings.sentry_dsn:
    sentry_sdk.init(dsn=settings.sentry_dsn, traces_sample_rate=0.1)


@asynccontextmanager
async def lifespan(app: FastAPI):
    device = "cuda" if torch.cuda.is_available() else "cpu"
    load_models(device=device)
    # Пул соединений Arq — кладём задачи индексации документов в Redis-очередь,
    # реальную работу делает отдельный процесс-воркер (src/worker.py), не этот.
    app.state.arq_pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    yield
    await app.state.arq_pool.close()


app = FastAPI(title="LegalSearch API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(search.router)
app.include_router(history.router)
app.include_router(collections.router)
app.include_router(documents.router)
app.include_router(health.router)
app.include_router(telemetry.router)


# Общий access-log на КАЖДЫЙ HTTP-запрос (метод/путь/статус/время, кто —
# если есть токен) в activity_events (event_type="api_request") — часть
# полного логирования действий пользователя, см. memory:
# project-legalsearch-log, запрос пользователя 2026-11-04. Тело запроса
# здесь НЕ читается и НЕ логируется — пароли на /auth/* защищены этим
# самим фактом, а не только _NEVER_LOG_KEYS. Подробный payload для
# конкретных действий (поиск, клики, ввод) пишут сами роутеры/эндпоинт
# /telemetry/events, этот middleware — только грубый общий след.
# Запись уходит в фон (asyncio.create_task), чтобы не добавлять задержку
# в ответ пользователю.
@app.middleware("http")
async def activity_log_middleware(request: Request, call_next):
    started = time.perf_counter()
    response = await call_next(request)
    duration_ms = round((time.perf_counter() - started) * 1000, 1)

    user_id = activity_log.user_id_from_auth_header(request.headers.get("authorization"))
    session_id = request.headers.get("x-session-id")
    asyncio.create_task(activity_log.log_event(
        "api_request",
        user_id=user_id,
        session_id=session_id,
        payload={
            "method": request.method,
            "path": request.url.path,
            "status_code": response.status_code,
            "duration_ms": duration_ms,
        },
    ))
    return response


@app.get("/")
async def root():
    return {"status": "ok", "service": "LegalSearch API"}


@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

