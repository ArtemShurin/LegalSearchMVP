"""
Backfill новых полей (doc_number, publish_date, registration_date, source_url)
для документов pravo.gov.ru, проиндексированных ДО того, как эти поля
появились в схеме (2026-10-23). Не переэмбеддит и не скачивает PDF —
только точечный апдейт payload по уже существующим point_id, см. урок про
дешёвый backfill в project_pravo_gov_dedup.md (без Filter, напрямую по id).

Запуск:
    python backfill_pravo_gov_extra_fields.py --dry-run
    python backfill_pravo_gov_extra_fields.py
"""

import argparse
import asyncio
import io
import sys

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import aiohttp
from tqdm import tqdm
from qdrant_client import QdrantClient
from qdrant_client.models import Filter, FieldCondition, MatchValue

from index_pravo_gov import (
    BASE_URL, QDRANT_URL, COLLECTION_NAME,
    CONSECUTIVE_FAILURES_THRESHOLD, COOLDOWN_SECONDS,
    fetch_page, parse_doc_meta,
)


def find_targets(client: QdrantClient) -> dict[str, list[str]]:
    """
    Возвращает {doc_id: [point_id, ...]} для всех точек source=pravo.gov.ru,
    у которых ещё нет source_url (т.е. проиндексированы до backfill'а).
    """
    targets: dict[str, list[str]] = {}
    offset = None
    # Забираем ВСЕ source=pravo.gov.ru и фильтруем "нет source_url" на своей
    # стороне — Qdrant не даёт "is not empty" напрямую в фильтре must.
    scroll_filter = Filter(must=[FieldCondition(key="source", match=MatchValue(value="pravo.gov.ru"))])

    while True:
        points, offset = client.scroll(
            collection_name=COLLECTION_NAME,
            scroll_filter=scroll_filter,
            limit=500,
            offset=offset,
            with_payload=["doc_id", "source_url"],
            with_vectors=False,
        )
        for p in points:
            if p.payload.get("source_url"):
                continue
            doc_id = p.payload.get("doc_id")
            if not doc_id:
                continue
            targets.setdefault(doc_id, []).append(str(p.id))
        if offset is None:
            break
    return targets


async def run(dry_run: bool):
    print("=== Backfill: doc_number/publish_date/registration_date/source_url для pravo.gov.ru ===\n")
    client = QdrantClient(url=QDRANT_URL, timeout=60)

    print("Собираю список документов без новых полей...")
    targets = find_targets(client)
    print(f"Найдено уникальных документов для backfill: {len(targets)}")
    print(f"Режим: {'dry-run (без записи)' if dry_run else 'запись в Qdrant'}\n")

    stats = {"processed": 0, "updated": 0, "not_found": 0, "parse_failed": 0}
    consecutive_failures = 0

    async with aiohttp.ClientSession() as session:
        for doc_id, point_ids in tqdm(targets.items(), desc="Документы"):
            stats["processed"] += 1

            if consecutive_failures >= CONSECUTIVE_FAILURES_THRESHOLD:
                tqdm.write(f"  {consecutive_failures} сбоев подряд — пауза {COOLDOWN_SECONDS}с")
                await asyncio.sleep(COOLDOWN_SECONDS)
                consecutive_failures = 0

            html = await fetch_page(session, f"{BASE_URL}/document/{doc_id}")
            if not html:
                stats["not_found"] += 1
                consecutive_failures += 1
                await asyncio.sleep(0.3)
                continue
            consecutive_failures = 0

            meta = parse_doc_meta(html, doc_id)
            if not meta.get("doc_number") and not meta.get("publish_date"):
                stats["parse_failed"] += 1
                continue

            payload = {
                "doc_number": meta.get("doc_number", ""),
                "publish_date": meta.get("publish_date", ""),
                "registration_date": meta.get("registration_date", ""),
                "source_url": f"{BASE_URL}/document/{doc_id}",
            }

            if not dry_run:
                client.set_payload(collection_name=COLLECTION_NAME, payload=payload, points=point_ids)
            stats["updated"] += 1
            await asyncio.sleep(0.2)

    print(f"\n✓ Готово!")
    print(f"  Обработано документов:     {stats['processed']:,}")
    print(f"  Обновлено:                 {stats['updated']:,}")
    print(f"  Страница не найдена:       {stats['not_found']:,}")
    print(f"  Не удалось распарсить:     {stats['parse_failed']:,}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Только посчитать, без записи в Qdrant")
    args = parser.parse_args()
    asyncio.run(run(args.dry_run))
