"""
Arq-воркер для индексации документов компании — заменяет FastAPI BackgroundTasks
(см. memory: project-backend-production-readiness, пункт 2). Отдельный процесс,
переживает рестарт API-контейнера; задачи персистентны в Redis, не в памяти
процесса — при падении воркера посреди задачи Arq её переберёт заново (retry),
а не молча потеряет.

Запуск (см. docker-compose.yml, сервис worker):
    arq src.worker.WorkerSettings
"""

import asyncio
import uuid

import torch
from arq.connections import RedisSettings

from src.config import settings
from src.database import AsyncSessionLocal
from src.repositories.document_repository import DocumentRepository
from src.services.document_indexer import index_document_sync
from ml.models import load_models


async def _on_startup(ctx):
    """
    Воркер — ОТДЕЛЬНЫЙ процесс от FastAPI-приложения (src/main.py), поэтому
    load_models() из lifespan там его не касается. Без этого хука
    index_document_sync() падал с "Модели не загружены" при первой же
    задаче — воспроизведено и исправлено 2026-10-28 на реальном прогоне.
    """
    device = "cuda" if torch.cuda.is_available() else "cpu"
    load_models(device=device)


async def index_document_task(ctx, doc_id: str, company_id: str, filename: str, file_path: str) -> int:
    """
    Сама задача. Файл читается с диска по file_path (не передаётся байтами
    через Redis — backend и worker делят один volume uploads_data, см.
    docker-compose.yml, так дешевле и не раздувает очередь большими payload).
    Возвращает число проиндексированных чанков (видно в логах Arq).
    """
    async with AsyncSessionLocal() as db:
        repo = DocumentRepository(db)
        try:
            with open(file_path, "rb") as f:
                pdf_bytes = f.read()
            chunks = await asyncio.to_thread(index_document_sync, doc_id, company_id, filename, pdf_bytes)
            await repo.set_status(uuid.UUID(doc_id), "indexed")
            await db.commit()
            return chunks
        except Exception as e:
            await repo.set_status(uuid.UUID(doc_id), "failed", str(e)[:2000])
            await db.commit()
            raise  # пробрасываем дальше — Arq учтёт как провал попытки и сделает retry


class WorkerSettings:
    functions = [index_document_task]
    on_startup = _on_startup
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    max_tries = 3  # retry при падении (например, Qdrant временно недоступен)
    job_timeout = 300  # 5 минут с запасом на большой PDF + эмбеддинг
