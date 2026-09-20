"""
Скрипт индексации правовой базы в Qdrant.

Запуск:
    python index_documents.py

Предварительно:
    pip install -r requirements.txt
    Qdrant должен быть запущен: docker compose up -d qdrant
"""

import uuid
import re
from tqdm import tqdm
from datasets import load_dataset
from sentence_transformers import SentenceTransformer
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct

QDRANT_URL = "http://localhost:6333"
COLLECTION_NAME = "public_docs"
EMBEDDING_MODEL = "intfloat/multilingual-e5-large"
VECTOR_SIZE = 1024

CHUNK_SIZE = 400      # слов в чанке
CHUNK_OVERLAP = 50    # слов перекрытия
BATCH_SIZE = 64       # чанков за один батч на GPU


def chunk_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Нарезает текст на перекрывающиеся чанки по словам."""
    words = text.split()
    if not words:
        return []
    chunks = []
    start = 0
    while start < len(words):
        end = start + chunk_size
        chunk = " ".join(words[start:end])
        chunks.append(chunk)
        if end >= len(words):
            break
        start += chunk_size - overlap
    return chunks


def clean_text(text: str) -> str:
    """Убирает лишние пробелы и переносы строк."""
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def create_collection(client: QdrantClient):
    existing = [c.name for c in client.get_collections().collections]
    if COLLECTION_NAME in existing:
        print(f"Коллекция '{COLLECTION_NAME}' уже существует, пропускаем создание.")
        return
    client.create_collection(
        collection_name=COLLECTION_NAME,
        vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
    )
    print(f"Коллекция '{COLLECTION_NAME}' создана.")


def get_indexed_count(client: QdrantClient) -> int:
    try:
        info = client.get_collection(COLLECTION_NAME)
        return info.vectors_count or 0
    except Exception:
        return 0


def index_batch(client: QdrantClient, model: SentenceTransformer, batch: list[dict]):
    """Эмбеддит и загружает батч чанков в Qdrant."""
    texts = [f"passage: {item['text']}" for item in batch]
    embeddings = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)

    points = []
    for i, item in enumerate(batch):
        points.append(PointStruct(
            id=str(uuid.uuid4()),
            vector=embeddings[i].tolist(),
            payload={
                "doc_id":   item["doc_id"],
                "title":    item["title"],
                "text":     item["text"],
                "doc_type": item["doc_type"],
                "date":     item["date"],
                "chunk_idx": item["chunk_idx"],
            }
        ))

    client.upsert(collection_name=COLLECTION_NAME, points=points)


def main():
    print("=== LegalSearch Indexer ===\n")

    # Подключение к Qdrant
    print("Подключаюсь к Qdrant...")
    client = QdrantClient(url=QDRANT_URL)
    create_collection(client)
    already_indexed = get_indexed_count(client)
    if already_indexed > 0:
        print(f"Уже проиндексировано {already_indexed:,} векторов.")

    # Загрузка модели
    print(f"\nЗагружаю модель {EMBEDDING_MODEL}...")
    print("(первый запуск скачает ~1.5 ГБ)\n")
    model = SentenceTransformer(EMBEDDING_MODEL, device="cuda")

    # Загрузка датасета
    print("Загружаю датасет RusLawOD...")
    print("(первый запуск скачает несколько ГБ)\n")
    dataset = load_dataset("irlspbru/RusLawOD", split="train", streaming=False)

    total_docs = len(dataset)
    print(f"Документов в датасете: {total_docs:,}\n")

    # Индексация
    batch = []
    total_chunks = 0

    for doc in tqdm(dataset, desc="Документы", total=total_docs):
        raw_text = doc.get("textIPS") or ""
        if not raw_text or len(raw_text.strip()) < 100:
            continue

        text = clean_text(raw_text)
        title = doc.get("headingIPS") or "Без названия"
        doc_id = str(doc.get("pravogovruNd") or uuid.uuid4())
        doc_type = str(doc.get("doc_typeIPS") or "НПА")
        date = str(doc.get("docdateIPS") or "")

        chunks = chunk_text(text)
        for idx, chunk in enumerate(chunks):
            batch.append({
                "doc_id":    doc_id,
                "title":     title,
                "text":      chunk,
                "doc_type":  doc_type,
                "date":      date,
                "chunk_idx": idx,
            })

            if len(batch) >= BATCH_SIZE:
                index_batch(client, model, batch)
                total_chunks += len(batch)
                batch = []

    # Остаток
    if batch:
        index_batch(client, model, batch)
        total_chunks += len(batch)

    print(f"\n✓ Готово! Загружено {total_chunks:,} чанков из {total_docs:,} документов.")
    print(f"Коллекция '{COLLECTION_NAME}' в Qdrant: http://localhost:6333/dashboard")


if __name__ == "__main__":
    main()
