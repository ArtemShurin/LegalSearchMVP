"""
Разовый бэкафилл: добавляет поле dedup_key уже проиндексированным точкам
RusLawOD в Qdrant, чтобы pravo.gov.ru мог дедуплицироваться против них
(см. dedup_key.py — почему нельзя просто сравнивать doc_id).

Строит dedup_key из doc_typeIPS + docNumberIPS + docdateIPS (те же поля,
из которых строился текст при первой индексации в index_documents.py)
и проставляет его через batch_update_points по фильтру doc_id == pravogovruNd —
без переэмбеддинга, только метаданные.

Запуск:
    python backfill_dedup_keys.py [--batch-size 200] [--limit N]
"""
import argparse
import io
import sys

if hasattr(sys.stdout, 'buffer'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

from datasets import load_dataset
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Filter, FieldCondition, MatchValue, PayloadSchemaType,
    SetPayload, SetPayloadOperation,
)
from tqdm import tqdm

from dedup_key import make_dedup_key

QDRANT_URL = "http://localhost:6333"
COLLECTION_NAME = "public_docs"


def build_operations(docs: list[dict]) -> list[SetPayloadOperation]:
    ops = []
    for doc in docs:
        doc_id = str(doc.get("pravogovruNd") or "")
        if not doc_id:
            continue
        key = make_dedup_key(
            doc.get("doc_typeIPS") or "",
            doc.get("docNumberIPS") or "",
            doc.get("docdateIPS") or "",
        )
        ops.append(SetPayloadOperation(set_payload=SetPayload(
            payload={"dedup_key": key},
            filter=Filter(must=[FieldCondition(key="doc_id", match=MatchValue(value=doc_id))]),
        )))
    return ops


def main(batch_size: int, limit: int | None):
    print("Подключаюсь к Qdrant...")
    client = QdrantClient(url=QDRANT_URL, timeout=60)

    try:
        client.create_payload_index(
            collection_name=COLLECTION_NAME,
            field_name="dedup_key",
            field_schema=PayloadSchemaType.KEYWORD,
        )
        print("Индекс по dedup_key создан.")
    except Exception:
        pass

    print("Стримлю RusLawOD и проставляю dedup_key...")
    ds = load_dataset("irlspbru/RusLawOD", split="train", streaming=True)

    buffer = []
    processed = 0
    skipped_no_id = 0
    pbar = tqdm(desc="Документы", unit="док")

    for doc in ds:
        if limit and processed >= limit:
            break
        buffer.append(doc)
        processed += 1
        pbar.update(1)

        if len(buffer) >= batch_size:
            ops = build_operations(buffer)
            skipped_no_id += len(buffer) - len(ops)
            if ops:
                client.batch_update_points(collection_name=COLLECTION_NAME, update_operations=ops)
            buffer = []

    if buffer:
        ops = build_operations(buffer)
        skipped_no_id += len(buffer) - len(ops)
        if ops:
            client.batch_update_points(collection_name=COLLECTION_NAME, update_operations=ops)

    pbar.close()
    print(f"\n✓ Готово. Обработано документов: {processed:,}")
    print(f"  Без pravogovruNd (пропущено): {skipped_no_id:,}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-size", type=int, default=200)
    parser.add_argument("--limit", type=int, default=None, help="Ограничить кол-во документов (для теста)")
    args = parser.parse_args()
    main(args.batch_size, args.limit)
