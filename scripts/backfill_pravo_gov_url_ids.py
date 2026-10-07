"""
Бэкафилл поля real_url_id для уже проиндексированных документов (в основном
RusLawOD) — чтобы в UI можно было давать кликabельную ссылку на реальный
документ на publication.pravo.gov.ru.

Проблема: у RusLawOD doc_id = легаси pravogovruNd (9-значный, старая система),
НЕ открывает документ на текущем сайте. У текущего сайта свой 16-значный ID.
Прямого маппинга между ID нет — сопоставляем по dedup_key (тип+номер+дата акта,
уже проставлен на 100% точек RusLawOD, см. backfill_dedup_keys.py).

Стратегия:
  1. Краулим ЛИСТИНГ publication.pravo.gov.ru за блок/период — собираем
     (nd=16-значный ID, dedup_key) для каждого документа. Переиспользует
     collect_documents() из index_pravo_gov.py (та же защита от троттлинга:
     паузы, повторы, cooldown после серии сбоев) — index_pravo_gov.py НЕ
     редактируется, только импортируется.
  2. Для каждого dedup_key ищем В ЭТОЙ ЖЕ Qdrant-коллекции все точки
     (чанки) с таким dedup_key, у которых ещё нет real_url_id.
  3. Точечно проставляем real_url_id = nd через set_payload по списку
     point_id (дёшево по I/O — см. урок в project_pravo_gov_dedup.md про
     filter-based SetPayload на больших коллекциях, так НЕ делаем).
  4. Побочный продукт того же обхода: акты, для которых не нашлось НИ ОДНОЙ
     точки в Qdrant (значит документ у нас вообще отсутствует) — пишем их
     реквизиты в очередь-файл pravo_gov_missing_docs.jsonl. Докачивает их
     ОТДЕЛЬНЫЙ скрипт fetch_missing_pravo_gov.py (по известному nd, без
     повторного обхода листинга) — задачи разделены намеренно.

Запуск (пилот — один блок, 2 года, именно так рекомендовано в плане):
    python backfill_pravo_gov_url_ids.py --block president --date-from 01.01.2024 --date-to 01.01.2026 --dry-run
"""

import argparse
import asyncio
import json
import os
import sys
import io

if hasattr(sys.stdout, 'buffer'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

import aiohttp
from tqdm import tqdm
from qdrant_client import QdrantClient
from qdrant_client.models import Filter, FieldCondition, MatchValue

from index_pravo_gov import collect_documents, FEDERAL_BLOCKS, QDRANT_URL, COLLECTION_NAME

BATCH_UPDATE_SIZE = 200  # точек за один set_payload-вызов

MISSING_DOCS_PATH = os.path.join(os.path.dirname(__file__), "pravo_gov_missing_docs.jsonl")


def _log_missing_doc(meta: dict, block: str):
    try:
        with open(MISSING_DOCS_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps({**meta, "block": block}, ensure_ascii=False) + "\n")
    except Exception:
        pass


def ensure_real_url_id_index(client: QdrantClient):
    from qdrant_client.models import PayloadSchemaType
    try:
        client.create_payload_index(
            collection_name=COLLECTION_NAME,
            field_name="real_url_id",
            field_schema=PayloadSchemaType.KEYWORD,
        )
    except Exception:
        pass  # уже существует


def find_points_without_url(client: QdrantClient, dedup_key: str) -> list[str]:
    """
    Точки с этим dedup_key, у которых ЕЩЁ НЕТ real_url_id (не перезаписываем
    то, что уже проставлено — дёшево пропустить уже сделанную работу).
    """
    point_ids = []
    offset = None
    while True:
        results, offset = client.scroll(
            collection_name=COLLECTION_NAME,
            scroll_filter=Filter(
                must=[FieldCondition(key="dedup_key", match=MatchValue(value=dedup_key))],
            ),
            limit=100,
            offset=offset,
            with_payload=["real_url_id"],
            with_vectors=False,
            timeout=30,
        )
        for p in results:
            if not p.payload.get("real_url_id"):
                point_ids.append(p.id)
        if offset is None:
            break
    return point_ids


async def main(block_key: str, date_from: str, date_to: str, dry_run: bool, limit_docs: int | None):
    print("=== Бэкафилл real_url_id (pravo.gov.ru) ===\n")
    blocks = FEDERAL_BLOCKS if block_key == "all" else [b for b in FEDERAL_BLOCKS if b[0] == block_key]
    if not blocks:
        print(f"ОШИБКА: неизвестный блок '{block_key}', доступны: {[b[0] for b in FEDERAL_BLOCKS]}")
        return

    print(f"Блоки: {[b[0] for b in blocks]}")
    print(f"Период: {date_from} — {date_to}")
    print(f"Режим:  {'dry-run (без записи в Qdrant)' if dry_run else 'запись в Qdrant'}\n")

    client = QdrantClient(url=QDRANT_URL, timeout=60)
    if not dry_run:
        ensure_real_url_id_index(client)

    stats = {"crawled": 0, "no_dedup_key": 0, "matched_points": 0, "no_match": 0, "updated_points": 0}

    async with aiohttp.ClientSession() as session:
        for block, block_name in blocks:
            print(f"\n[{block_name}]")
            docs = await collect_documents(session, block, date_from, date_to)
            print(f"  Собрано со страниц листинга: {len(docs)}")
            if limit_docs:
                docs = docs[:limit_docs]

            for meta in tqdm(docs, desc="  Сопоставление с Qdrant", leave=False):
                stats["crawled"] += 1
                nd = meta["doc_id"]
                dedup_key = meta["dedup_key"]

                if not dedup_key:
                    stats["no_dedup_key"] += 1
                    continue

                point_ids = find_points_without_url(client, dedup_key)
                if not point_ids:
                    stats["no_match"] += 1
                    _log_missing_doc(meta, block)
                    continue

                stats["matched_points"] += len(point_ids)
                if dry_run:
                    continue

                for i in range(0, len(point_ids), BATCH_UPDATE_SIZE):
                    chunk = point_ids[i:i + BATCH_UPDATE_SIZE]
                    client.set_payload(
                        collection_name=COLLECTION_NAME,
                        payload={"real_url_id": nd},
                        points=chunk,
                    )
                    stats["updated_points"] += len(chunk)

    print(f"\n{'='*60}")
    print("ИТОГИ")
    print(f"  Документов со страниц листинга:     {stats['crawled']:,}")
    print(f"  Без распознанных реквизитов:        {stats['no_dedup_key']:,}")
    print(f"  Не нашли соответствия в Qdrant:      {stats['no_match']:,}")
    print(f"  Точек (чанков) с совпадением:        {stats['matched_points']:,}")
    if not dry_run:
        print(f"  Точек реально обновлено:             {stats['updated_points']:,}")

    if stats["no_match"]:
        print(f"\n  → {stats['no_match']:,} актов без соответствия записаны в очередь:")
        print(f"    {MISSING_DOCS_PATH}")
        print(f"    Докачать отдельным скриптом: python fetch_missing_pravo_gov.py")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--block", default="president",
                         help="Блок (" + ", ".join(b[0] for b in FEDERAL_BLOCKS) + ", или 'all')")
    parser.add_argument("--date-from", required=True, help="ДД.ММ.ГГГГ")
    parser.add_argument("--date-to", required=True, help="ДД.ММ.ГГГГ")
    parser.add_argument("--dry-run", action="store_true",
                         help="Только посчитать совпадения, не писать в Qdrant")
    parser.add_argument("--limit-docs", type=int, default=None,
                         help="Максимум документов из листинга на блок (для теста)")
    args = parser.parse_args()

    asyncio.run(main(args.block, args.date_from, args.date_to, args.dry_run, args.limit_docs))
