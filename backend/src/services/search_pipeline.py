"""
Реальный AI-пайплайн поиска:
  1. Embed query (multilingual-e5-large) -> retrieve top-20 из Qdrant -> rerank top-5 (cross-encoder)
  2. Если топ-1 score ниже порога — переформулировка бытового запроса через LLM (Qwen2.5-7B) и повтор поиска
  3. Генерация финального ответа через LLM — стримится клиенту (см. routers/search.py), а не отсюда.

Пороги и промпты подобраны по результатам tests/test_*.py (см. memory: test-rewrite-comparison, test-strong-zone).
"""

import asyncio
import re
from qdrant_client.models import Filter, FieldCondition, MatchValue, IsEmptyCondition, PayloadField

from src.config import settings
from src.schemas.search import SearchRequest, SourceItem
from ml import retrieval
from ml import generation as llm

RETRIEVE_TOP_K = 20        # сколько кандидатов достаём из Qdrant
RERANK_TOP_K = 5           # сколько оставляем после reranker
RELEVANCE_THRESHOLD = 2.0  # ниже этого — запрос считается нерелевантным / нуждается в rewriting
COLLECTION = "public_docs"

# Порог для режима "искать только во внутренних документах" НАМЕРЕННО ниже обычного.
# Эмпирически (2026-10-01, тест на реальном загруженном документе): cross-encoder
# reranker обучен в основном на юридическом/обычном prose-контенте — для технического
# контента (таблицы схем БД, списки полей) он даёт систематически заниженные
# абсолютные score даже для явно релевантных чанков (наблюдались score вплоть до
# -0.98 для того, что реально отвечало на вопрос). Обычный порог 2.0 отсекал бы почти
# все такие совпадения. См. memory: project-backend-status.
INTERNAL_RELEVANCE_THRESHOLD = -2.0

# Чанки в Qdrant нарезаны по 400 слов (см. scripts/index_pravo_gov.py и
# scripts/index_documents.py chunk_text) — это может быть до ~3000+ символов.
# FRAGMENT_MAX_LEN — почти весь чанк целиком: карточка на фронте сама
# схлопывает превью до 2 строк через CSS, а разворачивает по клику пользователя.
FRAGMENT_MAX_LEN = 2000
FRAGMENT_SEARCH_WINDOW = 2500  # ищем границу предложения в пределах всего чанка, не только начала


def _clean_fragment(raw_text: str, max_len: int = FRAGMENT_MAX_LEN) -> str:
    """
    Сырой текст чанка часто начинается с середины фразы (нарезка по словам
    без учёта границ предложений). Ищем первую точку/!/?/; с пробелом и
    заглавной буквой/цифрой после — и начинаем цитату с неё. Если такой
    границы нет во всём доступном окне (чанк весь — один длинный кусок без
    ранних разделителей) — честно помечаем многоточием в начале, что это
    обрывок из середины, а не делаем вид, что текст начинается с начала.
    Аналогично помечаем многоточием конец, если обрезали раньше конца чанка.
    """
    window = raw_text[:FRAGMENT_SEARCH_WINDOW]
    match = re.search(r'[.!?;]\s+(?=[А-ЯA-Z0-9])', window[:max_len - 100])
    if match and match.start() >= 15:
        start = match.end()
        lead = ""
    else:
        start = 0
        lead = "…"

    end = start + max_len
    body = window[start:end].strip()
    trail = "…" if end < len(raw_text) else ""
    return f"{lead}{body}{trail}"


def _build_qdrant_filter(filters, company_id: str | None, internal_only: bool) -> Filter:
    """
    Переводит SearchFilters в Qdrant-фильтр + изоляция между компаниями.

    internal_only=False (по умолчанию): публичные документы (без company_id в
    payload) ИЛИ документы своей компании — обычный смешанный поиск.
    internal_only=True: ТОЛЬКО документы своей компании (company_id обязателен
    как must) — публичный корпус из 1.27М точек не участвует вообще. Это и
    есть переключатель "искать только во внутренних документах" из ТЗ —
    без него внутренние документы почти всегда проигрывают конкуренцию за
    top-20 с огромным публичным корпусом при обычных, не узкоспецифичных
    формулировках вопроса (см. memory: project-backend-status, тест 2026-10-01).
    """
    must = []
    if filters and filters.category:
        must.append(FieldCondition(key="doc_type", match=MatchValue(value=filters.category)))

    # date_from/date_to пока не поддержаны: в payload дата хранится строкой формата
    # "ДД.ММ.ГГГГ" (не ISO, не число) — Qdrant Range требует числовое поле, а
    # лексикографическое сравнение таких строк даёт неверный хронологический порядок.
    # Нужна переиндексация с датой в сортируемом формате, прежде чем включать фильтр.
    # Пока просто игнорируем date_from/date_to, чтобы не падать и не давать неверный результат.

    if internal_only:
        # Без company_id технически невозможно иметь свои документы — подставляем
        # заведомо несуществующее значение, чтобы гарантированно ничего не найти,
        # а не случайно утечь что-то при ошибочном вызове без company_id.
        must.append(FieldCondition(key="company_id", match=MatchValue(value=company_id or "__none__")))
        return Filter(must=must)

    should = [IsEmptyCondition(is_empty=PayloadField(key="company_id"))]
    if company_id:
        should.append(FieldCondition(key="company_id", match=MatchValue(value=company_id)))

    return Filter(must=must or None, should=should)


def _embed_retrieve_rerank(query: str, qdrant_filter: Filter | None) -> list[SourceItem]:
    """
    Один проход embed -> retrieve -> rerank (вся ML-логика — в ml/retrieval.py,
    см. legalsearch/ml/) -> top-K уникальных документов (по убыванию score).
    Дедуп по doc_id, обрезка под лимит колонки БД, чистка фрагмента для UI —
    прикладная логика, остаётся здесь, не в ML-пакете.
    """
    ranked = retrieval.search_and_rerank(
        query, COLLECTION, qdrant_filter,
        retrieve_top_k=RETRIEVE_TOP_K, qdrant_url=settings.qdrant_url,
    )
    if not ranked:
        return []

    sources: list[SourceItem] = []
    seen_docs = set()
    for score, hit in ranked:
        doc_id = hit.payload.get("doc_id", "")
        if doc_id in seen_docs:
            continue
        seen_docs.add(doc_id)
        sources.append(SourceItem(
            document_id=doc_id,
            title=hit.payload.get("title", "Без названия")[:500],  # DB: title VARCHAR(500)
            relevance=round(float(score), 4),
            fragment=_clean_fragment(hit.payload.get("text", "")),
        ))
        if len(sources) >= RERANK_TOP_K:
            break
    return sources


def _passes_threshold(sources: list[SourceItem], threshold: float) -> bool:
    return bool(sources) and sources[0].relevance >= threshold


def _resolve_sources_sync(request: SearchRequest, company_id: str | None) -> list[SourceItem]:
    """Прямой поиск +, если нужно, переформулировка через LLM. Возвращает
    пустой список, если ничего релевантного не нашлось."""
    qdrant_filter = _build_qdrant_filter(request.filters, company_id, request.internal_only)
    threshold = INTERNAL_RELEVANCE_THRESHOLD if request.internal_only else RELEVANCE_THRESHOLD

    sources = _embed_retrieve_rerank(request.query, qdrant_filter)

    # Прямой поиск хорошо работает на юридическом языке, но проваливается на бытовых
    # формулировках — в этом случае пробуем переформулировать запрос через LLM.
    if not _passes_threshold(sources, threshold):
        try:
            rewritten_query = llm.rewrite_query(request.query)
            rewritten_sources = _embed_retrieve_rerank(rewritten_query, qdrant_filter)
            if _passes_threshold(rewritten_sources, threshold):
                sources = rewritten_sources
        except llm.LLMUnavailableError:
            pass  # LLM недоступна — остаёмся с результатом прямого поиска

    return sources if _passes_threshold(sources, threshold) else []


async def resolve_sources(request: SearchRequest, company_id: str | None = None) -> list[SourceItem]:
    # Блокирующая работа (модели, Qdrant, HTTP к Ollama для rewrite) —
    # уводим в отдельный поток, чтобы не блокировать event loop FastAPI.
    return await asyncio.to_thread(_resolve_sources_sync, request, company_id)


def fallback_answer(query: str, sources: list[SourceItem]) -> str:
    """Используется, если стрим ответа оборвался до первого токена (LLM недоступна)."""
    titles = "; ".join(s.title for s in sources[:3])
    return (
        f'По запросу "{query}" найдено {len(sources)} релевантных документов: {titles}. '
        f"(Не удалось сгенерировать развёрнутый ответ — LLM временно недоступна.)"
    )
