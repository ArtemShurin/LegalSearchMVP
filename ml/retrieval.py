"""
Retrieval-пайплайн: embed query -> Qdrant search (top-K) -> rerank (cross-encoder).
Чистая ML-логика — как считается score. Прикладные вещи (дедуп документов,
обрезка под лимиты БД, чистка фрагментов текста для UI) здесь намеренно НЕ
делаются — это backend/src/services/search_pipeline.py поверх этого модуля.
"""

import os

from qdrant_client import QdrantClient
from qdrant_client.models import Filter

from ml.models import get_embedder, get_reranker

QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")


def embed_query(query: str) -> list[float]:
    """multilingual-e5-large ожидает префикс 'query: '/'passage: ' для
    разных ролей текста (так модель обучалась, см. её карточку на HF)."""
    embedder = get_embedder()
    return embedder.encode(f"query: {query}", normalize_embeddings=True).tolist()


def search_and_rerank(
    query: str,
    collection: str,
    qdrant_filter: Filter | None = None,
    retrieve_top_k: int = 20,
    qdrant_url: str = QDRANT_URL,
) -> list[tuple[float, dict]]:
    """
    Возвращает ВСЕ retrieve_top_k кандидатов с их score после rerank,
    отсортированные по убыванию score — (score, hit) пары. Дедуп по
    документу и отбор финального top-K — забота вызывающего кода
    (в одном документе может быть несколько чанков среди кандидатов).
    """
    embedder = get_embedder()
    reranker = get_reranker()
    client = QdrantClient(url=qdrant_url, timeout=30)

    query_vector = embed_query(query)

    hits = client.search(
        collection_name=collection,
        query_vector=query_vector,
        limit=retrieve_top_k,
        query_filter=qdrant_filter,
        with_payload=True,
    )
    if not hits:
        return []

    pairs = [(query, hit.payload.get("text", "")) for hit in hits]
    scores = reranker.predict(pairs)
    return sorted(zip(scores, hits), key=lambda x: x[0], reverse=True)
