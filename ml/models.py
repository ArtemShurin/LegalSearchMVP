"""
Singleton-загрузка ML-моделей (эмбеддинг + reranker). Модели живут в памяти
весь цикл работы процесса, который их загрузил (backend при старте,
скрипты индексации — по требованию).
"""

from sentence_transformers import SentenceTransformer, CrossEncoder

EMBEDDING_MODEL = "intfloat/multilingual-e5-large"
RERANKER_MODEL = "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1"

_embedder: SentenceTransformer | None = None
_reranker: CrossEncoder | None = None


def load_models(device: str = "cpu") -> None:
    global _embedder, _reranker
    print(f"Загружаю embedder ({EMBEDDING_MODEL})...")
    _embedder = SentenceTransformer(EMBEDDING_MODEL, device=device)
    print(f"Загружаю reranker ({RERANKER_MODEL})...")
    _reranker = CrossEncoder(RERANKER_MODEL, device=device)
    print("Модели загружены.")


def get_embedder() -> SentenceTransformer:
    if _embedder is None:
        raise RuntimeError("Модели не загружены. Вызови load_models() при старте.")
    return _embedder


def get_reranker() -> CrossEncoder:
    if _reranker is None:
        raise RuntimeError("Модели не загружены. Вызови load_models() при старте.")
    return _reranker
