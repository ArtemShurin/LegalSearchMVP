"""
Индексация документов, загруженных компаниями напрямую через API.
Текст/чанкинг — тот же подход, что в scripts/index_pravo_gov.py (chunk_text,
400 слов с перехлёстом 50), но без OCR: корпоративные PDF почти всегда с
текстовым слоем, EasyOCR — тяжёлая GPU-зависимость, не тащим её в бэкенд
без реальной необходимости (см. memory: project-frontend-design).
"""

import re
import uuid
from datetime import datetime

import fitz  # PyMuPDF
from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct

from src.config import settings
from ml.models import get_embedder

COLLECTION = "public_docs"
CHUNK_SIZE = 400
CHUNK_OVERLAP = 50


class DocumentIndexError(Exception):
    """Не удалось извлечь текст или проиндексировать документ."""


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def extract_pdf_text(pdf_bytes: bytes) -> str:
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        pages = [page.get_text() for page in doc]
        return _clean_text("\n".join(pages))
    except Exception as e:
        raise DocumentIndexError(f"Не удалось прочитать PDF: {e}") from e


def chunk_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    words = text.split()
    if not words:
        return []
    chunks = []
    start = 0
    while start < len(words):
        end = start + chunk_size
        chunks.append(" ".join(words[start:end]))
        if end >= len(words):
            break
        start += chunk_size - overlap
    return chunks


def index_document_sync(doc_id: str, company_id: str, filename: str, pdf_bytes: bytes) -> int:
    """
    Синхронная блокирующая индексация (PDF parsing, embedding, upsert) —
    вызывается из фоновой задачи через asyncio.to_thread, не из event loop.
    Возвращает число проиндексированных чанков.
    """
    text = extract_pdf_text(pdf_bytes)
    if not text:
        raise DocumentIndexError("В документе не найдено текста (похоже на скан без текстового слоя)")

    chunks = chunk_text(text)
    if not chunks:
        raise DocumentIndexError("Не удалось разбить документ на чанки")

    embedder = get_embedder()
    client = QdrantClient(url=settings.qdrant_url, timeout=60)

    texts = [f"passage: {c}" for c in chunks]
    embeddings = embedder.encode(texts, normalize_embeddings=True, show_progress_bar=False)

    today = datetime.utcnow().strftime("%d.%m.%Y")
    points = [
        PointStruct(
            id=str(uuid.uuid4()),
            vector=embeddings[i].tolist(),
            payload={
                "doc_id": doc_id,
                "company_id": company_id,
                "title": filename,
                "text": chunk,
                "doc_type": "Документ компании",
                "date": today,
                "chunk_idx": i,
                "source": "company_upload",
            },
        )
        for i, chunk in enumerate(chunks)
    ]
    client.upsert(collection_name=COLLECTION, points=points)
    return len(points)


def delete_document_points(doc_id: str) -> None:
    """Удаляет все чанки документа из Qdrant по doc_id (при удалении документа компанией)."""
    from qdrant_client.models import Filter, FieldCondition, MatchValue

    client = QdrantClient(url=settings.qdrant_url, timeout=30)
    client.delete(
        collection_name=COLLECTION,
        points_selector=Filter(must=[FieldCondition(key="doc_id", match=MatchValue(value=doc_id))]),
    )
