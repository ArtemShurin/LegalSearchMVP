import asyncio
import json
import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.models.user import User
from src.schemas.search import SearchRequest
from src.services import search_pipeline, activity_log
from src.services.rate_limit import enforce_search_rate_limit
from src.services.metrics import search_requests_total
from ml import generation as llm
from src.repositories.query_repository import QueryRepository
from src.routers.auth import get_current_user

router = APIRouter(prefix="/search", tags=["search"])


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.post("/")
async def search(
    http_request: Request,
    request: SearchRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # До открытия SSE-стрима — если упадёт 429, клиент получит обычный JSON-
    # ответ с ошибкой, а не оборванный поток событий.
    await enforce_search_rate_limit(
        http_request.app.state.arq_pool,
        str(current_user.company_id) if current_user.company_id else None,
        str(current_user.id),
    )

    async def event_stream():
        company_id = str(current_user.company_id) if current_user.company_id else None
        session_id = http_request.headers.get("x-session-id")
        _t_start = time.perf_counter()

        sources, pipeline_debug = await search_pipeline.resolve_sources(request, company_id)
        yield _sse("sources", {"sources": [s.model_dump() for s in sources]})

        answer_parts: list[str] = []
        outcome = "unknown"

        if not sources:
            text = (
                "Среди ваших внутренних документов ничего не найдено. Попробуйте "
                "переформулировать вопрос или загрузить документ, если ещё не сделали это."
                if request.internal_only else
                "По вашему запросу ничего не найдено в базе нормативных актов. "
                "Попробуйте переформулировать вопрос."
            )
            answer_parts.append(text)
            yield _sse("token", {"text": text})
            outcome = "no_sources"
            search_requests_total.labels(outcome=outcome).inc()
        else:
            queue_position = llm.llm_queue_position()
            if queue_position > 0:
                # Явная обратная связь вместо тихого зависания — H-DEV3, см. memory:
                # project-backend-production-readiness. Позиция приблизительная
                # (между проверкой и реальным acquire очередь могла измениться),
                # но честно отражает текущую нагрузку, а не притворяется, что LLM
                # свободна.
                yield _sse("queue", {"position": queue_position})
            try:
                async for chunk in llm.stream_answer(request.query, sources):
                    if llm.contains_cjk(chunk):
                        # Известный сбой локальной модели — уверенно "съезжает" в текст на
                        # китайском (вплоть до цитирования статей ГК КНР вместо ГК РФ).
                        # Останавливаем генерацию сразу, а не даём дописать фейковый ответ.
                        warning = (
                            "\n\n⚠️ Генерация остановлена: модель начала выдавать текст на "
                            "другом языке (известная ошибка локальной LLM). Пожалуйста, "
                            "свяжитесь с результатом осторожно и сверьтесь с источниками ниже."
                        )
                        answer_parts.append(warning)
                        yield _sse("token", {"text": warning})
                        outcome = "cjk_aborted"
                        search_requests_total.labels(outcome=outcome).inc()
                        break
                    answer_parts.append(chunk)
                    yield _sse("token", {"text": chunk})
                else:
                    outcome = "answered"
                    search_requests_total.labels(outcome=outcome).inc()
            except llm.LLMUnavailableError:
                outcome = "llm_unavailable"
                search_requests_total.labels(outcome=outcome).inc()
                if not answer_parts:
                    text = search_pipeline.fallback_answer(request.query, sources)
                    answer_parts.append(text)
                    yield _sse("token", {"text": text})
                # Если LLM упала уже после первых токенов — просто останавливаемся,
                # клиент получит частичный ответ, откатывать уже отправленное нельзя.

        full_answer = "".join(answer_parts)

        # Отдельное детальное событие (не api_request-access-log) — именно
        # сюда смотреть при разборе "запрос не дал релевантного результата":
        # видно, прошёл ли прямой поиск порог, какой была переформулировка
        # (если была), её собственные источники/оценки, тайминги по стадиям,
        # и финальный исход — по явному запросу пользователя 2026-11-04.
        asyncio.create_task(activity_log.log_event(
            "search_pipeline",
            user_id=str(current_user.id),
            company_id=company_id,
            session_id=session_id,
            payload={
                "query": request.query,
                "internal_only": request.internal_only,
                "outcome": outcome,
                "total_ms": round((time.perf_counter() - _t_start) * 1000, 1),
                "answer_len": len(full_answer),
                **pipeline_debug,
            },
        ))

        query_repo = QueryRepository(db)
        query = await query_repo.create(
            user_id=current_user.id,
            text=request.query,
            answer=full_answer,
            filters=request.filters.model_dump() if request.filters else None,
        )
        await query_repo.add_sources(query.id, [s.model_dump() for s in sources])
        await db.commit()

        yield _sse("done", {"query_id": str(query.id)})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
