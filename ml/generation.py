"""
Клиент Ollama: переформулировка бытовых запросов и генерация ответа (Qwen2.5-7B).
Промпты идентичны проверенным в tests/test_rewrite_comparison.py и tests/test_strong_zone.py.
"""

import asyncio
import json
import os
import re
import threading
from collections.abc import AsyncIterator

import httpx
import urllib.request
import urllib.error

from ml.metrics import llm_request_duration_seconds, llm_queue_length

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
LLM_MODEL = os.environ.get("LLM_MODEL", "qwen2.5:7b")

# Очередь на LLM-инференс (H-DEV3, см. memory: project-backend-production-readiness).
# Ollama крутит одну модель на ОДНОЙ GPU — без этого параллельные запросы просто
# встают в очередь CUDA без видимости для пользователя (зависший запрос без
# обратной связи). LLM_CONCURRENCY=1 по умолчанию — ровно одна GPU, один слот.
# threading.Semaphore (не asyncio.Semaphore) — потому что эта очередь обслуживает
# ДВА разных места вызова Ollama с разной моделью исполнения: rewrite_query()
# синхронный (вызывается через asyncio.to_thread из search_pipeline — не на event
# loop), stream_answer() асинхронный генератор. threading.Semaphore работает
# одинаково из обоих контекстов, asyncio.Semaphore — только из event loop.
LLM_CONCURRENCY = int(os.environ.get("LLM_CONCURRENCY", "1"))
_llm_semaphore = threading.Semaphore(LLM_CONCURRENCY)
_queue_lock = threading.Lock()
_queue_waiting = 0


def llm_queue_position() -> int:
    """Сколько запросов сейчас ждут своей очереди на LLM (для обратной связи пользователю)."""
    with _queue_lock:
        return _queue_waiting


def _acquire_llm_slot():
    global _queue_waiting
    with _queue_lock:
        _queue_waiting += 1
        llm_queue_length.set(_queue_waiting)
    try:
        _llm_semaphore.acquire()
    finally:
        with _queue_lock:
            _queue_waiting -= 1
            llm_queue_length.set(_queue_waiting)


def _release_llm_slot():
    _llm_semaphore.release()

# Известная проблема локальной квантизованной Qwen2.5-7B: иногда уверенно
# "съезжает" в генерацию на китайском (вплоть до цитирования реальных статей
# ГК КНР как будто это российское право) — см. memory: test-rewrite-comparison.
_CJK_PATTERN = re.compile(r'[一-鿿㐀-䶿豈-﫿]')


def contains_cjk(text: str) -> bool:
    return bool(_CJK_PATTERN.search(text))

REWRITE_PROMPT = """Ты — помощник юриста. Переформулируй запрос пользователя в юридические КЛЮЧЕВЫЕ СЛОВА для поиска по базе нормативных правовых актов России.

Правила:
- Верни ТОЛЬКО 5-10 ключевых юридических слов через пробел, без вопросов и предложений
- Используй существительные в именительном/родительном падеже
- Используй термины из российского законодательства (с указанием кодекса/статьи если очевидно)
- Не пиши вопросы, союзы, пояснения

Примеры:
Запрос: "сосед затопил квартиру кто платит"
Ключевые слова: возмещение ущерба залитие жилого помещения ответственность собственника ГК РФ статья 1064

Запрос: "работодатель не платит зарплату 3 месяца"
Ключевые слова: задержка выплаты заработной платы материальная ответственность работодателя статья 236 ТК РФ

Запрос: "врач допустил ошибку при операции"
Ключевые слова: врачебная ошибка медицинская халатность ответственность медицинской организации возмещение вреда здоровью

Запрос: "соседи шумят ночью что делать"
Ключевые слова: нарушение тишины ночное время КоАП РФ статья 6.3 административная ответственность шум жилой дом

Запрос: "партнёр вывел деньги из общего бизнеса"
Ключевые слова: вывод активов участником ООО ущерб обществу субсидиарная ответственность статья 53.1 ГК РФ

Запрос: {query}
Ключевые слова:"""

ANSWER_PROMPT = """Ты — юридический ассистент. Отвечай строго на основе предоставленных документов. Если ответа в документах нет — скажи об этом прямо.

Правила ответа:
- Отвечай по существу, 3-5 предложений
- Ссылайся на документы в формате [1], [2] и т.д.
- Указывай конкретные статьи и нормы если они есть в документах
- Не придумывай информацию которой нет в документах
- Пиши на русском языке

Документы:
{documents}

Вопрос: {query}

Ответ:"""


class LLMUnavailableError(Exception):
    """Ollama недоступна или вернула ошибку."""


def _ollama_generate(prompt: str, num_predict: int, call_type: str) -> str:
    payload = json.dumps({
        "model": LLM_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.1, "num_predict": num_predict},
    }).encode()
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    _acquire_llm_slot()
    try:
        # Метрика — только время самого вызова Ollama, БЕЗ ожидания в очереди
        # (то меряет llm_queue_length отдельно) — иначе гистограмма "длительности
        # вызова" на самом деле мерила бы совокупную нагрузку, а не сам вызов.
        with llm_request_duration_seconds.labels(call_type=call_type).time():
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read())
                return data.get("response", "").strip()
    except (urllib.error.URLError, TimeoutError) as e:
        raise LLMUnavailableError(str(e)) from e
    finally:
        _release_llm_slot()


def rewrite_query(query: str) -> str:
    """Переформулирует бытовой запрос в юридические ключевые слова. Вызывается
    из синхронного потока ретрива (см. search_pipeline._resolve_sources_sync),
    поэтому остаётся блокирующим — это не на пути стриминга к клиенту.
    Проходит через ту же очередь на LLM, что и stream_answer (_ollama_generate)."""
    return _ollama_generate(REWRITE_PROMPT.format(query=query), num_predict=128, call_type="rewrite")


def _build_answer_prompt(query: str, sources: list) -> str:
    docs_text = ""
    for i, s in enumerate(sources, 1):
        docs_text += f"[{i}] {s.title}\n{s.fragment}\n\n"
    return ANSWER_PROMPT.format(documents=docs_text.strip(), query=query)


async def stream_answer(query: str, sources: list) -> AsyncIterator[str]:
    """Генерирует финальный ответ потоково — yield'ит текст по мере прихода токенов от Ollama.
    Проходит через ту же очередь на LLM, что и rewrite_query — acquire/release семафора
    уводим в отдельный поток (asyncio.to_thread), чтобы ожидание слота не блокировало
    event loop (другие запросы продолжают обслуживаться, пока этот ждёт очереди)."""
    prompt = _build_answer_prompt(query, sources)
    payload = {
        "model": LLM_MODEL,
        "prompt": prompt,
        "stream": True,
        "options": {"temperature": 0.1, "num_predict": 512},
    }
    await asyncio.to_thread(_acquire_llm_slot)
    try:
        # .time() меряет весь async-блок целиком (сам контекст-менеджер
        # синхронный, просто засекает время входа/выхода — для yield-генератора
        # это ровно "от начала запроса до последнего токена", что и нужно).
        with llm_request_duration_seconds.labels(call_type="answer_stream").time():
            async with httpx.AsyncClient(timeout=120) as client:
                async with client.stream("POST", f"{OLLAMA_URL}/api/generate", json=payload) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if not line:
                            continue
                        chunk = json.loads(line)
                        text = chunk.get("response", "")
                        if text:
                            yield text
                        if chunk.get("done"):
                            break
    except httpx.HTTPError as e:
        raise LLMUnavailableError(str(e)) from e
    finally:
        _release_llm_slot()


def generate_answer(query: str, sources: list) -> str:
    """Нестримящий вариант — используется как fallback, если стрим оборвался
    до получения первого токена (тогда отдаём обычный ответ одним куском)."""
    return _ollama_generate(_build_answer_prompt(query, sources), num_predict=512, call_type="answer_fallback")
