import json

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.models.user import User
from src.schemas.search import SearchRequest
from src.services import search_pipeline
from ml import generation as llm
from src.repositories.query_repository import QueryRepository
from src.routers.auth import get_current_user

router = APIRouter(prefix="/search", tags=["search"])


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.post("/")
async def search(
    request: SearchRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    async def event_stream():
        company_id = str(current_user.company_id) if current_user.company_id else None
        sources = await search_pipeline.resolve_sources(request, company_id)
        yield _sse("sources", {"sources": [s.model_dump() for s in sources]})

        answer_parts: list[str] = []

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
        else:
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
                        break
                    answer_parts.append(chunk)
                    yield _sse("token", {"text": chunk})
            except llm.LLMUnavailableError:
                if not answer_parts:
                    text = search_pipeline.fallback_answer(request.query, sources)
                    answer_parts.append(text)
                    yield _sse("token", {"text": text})
                # Если LLM упала уже после первых токенов — просто останавливаемся,
                # клиент получит частичный ответ, откатывать уже отправленное нельзя.

        full_answer = "".join(answer_parts)

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
