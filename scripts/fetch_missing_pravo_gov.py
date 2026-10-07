"""
Точечная докачка документов pravo.gov.ru, накопленных в очереди
pravo_gov_missing_docs.jsonl скриптом backfill_pravo_gov_url_ids.py.

Это документы, которых у нас нет в Qdrant вообще (не нашлось совпадения по
dedup_key при обходе листинга) — их реквизиты и nd уже известны, поэтому
скачиваем PDF напрямую по nd, БЕЗ повторного обхода листинга сайта.

Запуск:
    python fetch_missing_pravo_gov.py --dry-run
    python fetch_missing_pravo_gov.py --limit 50
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

from index_pravo_gov import (
    QDRANT_URL, BASE_URL, EMBEDDING_MODEL, BATCH_SIZE,
    fetch_bytes, extract_pdf_text, chunk_text, index_batch, doc_exists,
    CONSECUTIVE_FAILURES_THRESHOLD, COOLDOWN_SECONDS,
)

MISSING_DOCS_PATH = os.path.join(os.path.dirname(__file__), "pravo_gov_missing_docs.jsonl")


def _load_missing_docs() -> list[dict]:
    """Схлопывает дубли по nd — один акт мог попасться в нескольких
    прогонах/блоках backfill_pravo_gov_url_ids.py."""
    if not os.path.exists(MISSING_DOCS_PATH):
        return []
    seen = {}
    with open(MISSING_DOCS_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            seen[d["doc_id"]] = d
    return list(seen.values())


async def main(dry_run: bool, limit: int | None):
    docs = _load_missing_docs()
    print("=== Докачка пропущенных документов pravo.gov.ru ===\n")
    print(f"В очереди: {len(docs)} актов (файл: {MISSING_DOCS_PATH})")
    print(f"Режим: {'dry-run (без индексации)' if dry_run else 'полная индексация'}\n")
    if not docs:
        return
    if limit:
        docs = docs[:limit]

    client = QdrantClient(url=QDRANT_URL, timeout=60)
    model = None
    if not dry_run:
        from sentence_transformers import SentenceTransformer
        print(f"Загружаю модель {EMBEDDING_MODEL}...")
        model = SentenceTransformer(EMBEDDING_MODEL, device="cuda")

    stats = {"processed": 0, "indexed": 0, "skipped_dup": 0, "skipped_no_pdf": 0,
              "skipped_empty": 0, "chunks": 0}
    batch = []
    consecutive_failures = 0

    async with aiohttp.ClientSession() as session:
        for meta in tqdm(docs, desc="Докачка"):
            stats["processed"] += 1
            nd = meta["doc_id"]

            # doc_exists() — защита на случай, если акт уже успели
            # проиндексировать другим путём между записью в очередь и докачкой
            if doc_exists(client, meta.get("dedup_key")):
                stats["skipped_dup"] += 1
                continue

            if consecutive_failures >= CONSECUTIVE_FAILURES_THRESHOLD:
                tqdm.write(f"  {consecutive_failures} сбоев подряд — пауза {COOLDOWN_SECONDS}с")
                await asyncio.sleep(COOLDOWN_SECONDS)
                consecutive_failures = 0

            pdf_bytes = await fetch_bytes(session, f"{BASE_URL}/file/pdf?eoNumber={nd}")
            if not pdf_bytes:
                stats["skipped_no_pdf"] += 1
                consecutive_failures += 1
                await asyncio.sleep(0.5)
                continue
            consecutive_failures = 0

            text, _used_ocr = extract_pdf_text(pdf_bytes)
            if len(text) < 100:
                stats["skipped_empty"] += 1
                continue

            if dry_run:
                stats["indexed"] += 1
                continue

            chunks = chunk_text(text)
            for idx, chunk in enumerate(chunks):
                batch.append({
                    "doc_id": nd, "title": meta.get("title", ""), "text": chunk,
                    "doc_type": meta.get("doc_type", "НПА"), "date": meta.get("date", ""),
                    "chunk_idx": idx, "dedup_key": meta.get("dedup_key") or "",
                })
                stats["chunks"] += 1
                if len(batch) >= BATCH_SIZE:
                    index_batch(client, model, batch)
                    batch = []
            stats["indexed"] += 1
            await asyncio.sleep(0.5)

    if batch and not dry_run:
        index_batch(client, model, batch)

    print(f"\n{'='*60}")
    print("ИТОГИ ДОКАЧКИ")
    print(f"  Обработано:               {stats['processed']:,}")
    print(f"  Проиндексировано:         {stats['indexed']:,}")
    print(f"  Пропущено (дубли):        {stats['skipped_dup']:,}")
    print(f"  Пропущено (нет PDF):      {stats['skipped_no_pdf']:,}")
    print(f"  Пропущено (пустой текст): {stats['skipped_empty']:,}")
    print(f"  Загружено чанков:         {stats['chunks']:,}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                         help="Скачать и распознать, но не индексировать в Qdrant")
    parser.add_argument("--limit", type=int, default=None,
                         help="Максимум документов из очереди (для теста)")
    args = parser.parse_args()

    asyncio.run(main(args.dry_run, args.limit))
