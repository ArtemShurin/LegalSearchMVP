"""
Индексация документов pravo.gov.ru (регион, по умолчанию region50 —
Московская область) с текстовым слоем — по уже построенному каталогу
(build_pravo_gov_catalog.py), без повторного сбора листинга.

OCR здесь НЕ запускается в обычном режиме — все строки каталога уже
отмечены как has_text_layer=True (PyMuPDF дал >=100 символов на момент
построения каталога). Если при реальном скачивании PDF внезапно окажется
сканом (редкий краевой случай — документ могли переопубликовать) —
документ пропускается, а не докатывается до OCR: сайт мог c тех пор
измениться, но раздувать OCR-нагрузку там, где её не планировали, не нужно.

Запуск:
    # Разведка — без индексации, без модели эмбеддингов
    python index_pravo_gov_catalog_text.py --dry-run --limit 20

    # Боевой прогон
    python index_pravo_gov_catalog_text.py
"""

import argparse
import asyncio
import csv
import io
import os
import sys
from pathlib import Path

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import aiohttp
from tqdm import tqdm
from sentence_transformers import SentenceTransformer
from qdrant_client import QdrantClient

from index_pravo_gov import (
    BASE_URL, QDRANT_URL, COLLECTION_NAME, EMBEDDING_MODEL, BATCH_SIZE,
    CONSECUTIVE_FAILURES_THRESHOLD, COOLDOWN_SECONDS,
    fetch_bytes, extract_pdf_text, chunk_text, doc_exists, ensure_payload_index, index_batch,
)

REGION_NAMES = {"region50": "Московская область"}
DEFAULT_CATALOG = Path(__file__).resolve().parent.parent / "data" / "npa" / "pravo_gov_catalog_region50.csv"


def load_catalog(path: str) -> tuple[list[str], list[dict]]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        all_rows = list(reader)
    return fieldnames, all_rows


def save_catalog(path: str, fieldnames: list[str], all_rows: list[dict]) -> None:
    """
    Перезаписывает каталог целиком (атомарно — через временный файл +
    os.replace, чтобы сбой посреди записи не повредил файл). Вызывается
    периодически по ходу индексации, не на каждый документ — иначе
    перезапись ~11 тыс. строк на каждый из 2447 документов была бы O(n²).
    """
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(all_rows)
    os.replace(tmp_path, path)


SAVE_EVERY = 20  # как часто перезаписывать каталог с обновлённым in_database


async def run(catalog_path: str, region: str, offset: int, limit: int | None, dry_run: bool):
    fieldnames, all_rows = load_catalog(catalog_path)
    text_rows = [r for r in all_rows if r["has_text_layer"] == "True"]
    rows = text_rows[offset:offset + limit] if limit is not None else text_rows[offset:]
    catalog_dirty_count = 0

    print("=== LegalSearch: индексация pravo.gov.ru по каталогу (текстовый слой) ===\n")
    print(f"Каталог: {catalog_path}")
    print(f"Регион:  {region} ({REGION_NAMES.get(region, '?')})")
    print(f"Срез:    [{offset}:{offset + limit if limit is not None else ''}] из {len(text_rows)} с текстовым слоем")
    print(f"В этом прогоне: {len(rows)} документов")
    print(f"Режим:   {'dry-run (без индексации)' if dry_run else 'полная индексация'}\n")

    print("Подключаюсь к Qdrant...")
    client = QdrantClient(url=QDRANT_URL, timeout=60)
    ensure_payload_index(client)

    model = None
    if not dry_run:
        print(f"Загружаю модель {EMBEDDING_MODEL}...")
        model = SentenceTransformer(EMBEDDING_MODEL, device="cuda")

    stats = {"processed": 0, "indexed": 0, "skipped_dup": 0, "skipped_no_pdf": 0,
              "skipped_empty": 0, "skipped_ocr_needed": 0, "chunks": 0}
    batch = []
    consecutive_failures = 0

    async with aiohttp.ClientSession() as session:
        for row in tqdm(rows, desc="Документы"):
            stats["processed"] += 1
            dedup_key = row["dedup_key"] or None

            if not dry_run and doc_exists(client, dedup_key):
                stats["skipped_dup"] += 1
                if row.get("in_database") != "True":
                    row["in_database"] = "True"
                    catalog_dirty_count += 1
                continue

            if consecutive_failures >= CONSECUTIVE_FAILURES_THRESHOLD:
                tqdm.write(f"  {consecutive_failures} сбоев подряд — пауза {COOLDOWN_SECONDS}с")
                await asyncio.sleep(COOLDOWN_SECONDS)
                consecutive_failures = 0

            pdf_bytes = await fetch_bytes(session, f"{BASE_URL}/file/pdf?eoNumber={row['doc_id']}")
            if not pdf_bytes:
                stats["skipped_no_pdf"] += 1
                consecutive_failures += 1
                await asyncio.sleep(0.5)
                continue
            consecutive_failures = 0

            text, used_ocr = extract_pdf_text(pdf_bytes)
            if used_ocr:
                stats["skipped_ocr_needed"] += 1
                continue
            if len(text) < 100:
                stats["skipped_empty"] += 1
                continue

            chunks = chunk_text(text)
            for idx, chunk in enumerate(chunks):
                batch.append({
                    "doc_id": row["doc_id"],
                    "title": row["title"],
                    "text": chunk,
                    "doc_type": row["doc_type"],
                    "date": row["signing_date"],
                    "chunk_idx": idx,
                    "dedup_key": dedup_key or "",
                    "block_type_for_laws": "region",
                    "block_name_for_laws": REGION_NAMES.get(region, region),
                    "block_code_for_laws": region,
                    "doc_number": row.get("doc_number", ""),
                    "publish_date": row.get("publish_date", ""),
                    "registration_date": row.get("registration_date", ""),
                    "source_url": f"{BASE_URL}/document/{row['doc_id']}",
                })
                stats["chunks"] += 1

                if not dry_run and len(batch) >= BATCH_SIZE:
                    index_batch(client, model, batch)
                    batch = []

            stats["indexed"] += 1
            if not dry_run:
                row["in_database"] = "True"
                catalog_dirty_count += 1
                if catalog_dirty_count >= SAVE_EVERY:
                    save_catalog(catalog_path, fieldnames, all_rows)
                    catalog_dirty_count = 0
            await asyncio.sleep(0.3)

    if batch and not dry_run:
        index_batch(client, model, batch)

    if not dry_run and catalog_dirty_count > 0:
        save_catalog(catalog_path, fieldnames, all_rows)

    print(f"\n✓ Готово!")
    print(f"  Обработано:                {stats['processed']:,}")
    print(f"  Проиндексировано:          {stats['indexed']:,}")
    print(f"  Пропущено (дубли):         {stats['skipped_dup']:,}")
    print(f"  Пропущено (нет PDF):       {stats['skipped_no_pdf']:,}")
    print(f"  Пропущено (пустой текст):  {stats['skipped_empty']:,}")
    print(f"  Пропущено (внезапно скан): {stats['skipped_ocr_needed']:,}")
    print(f"  Загружено чанков:          {stats['chunks']:,}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", default=str(DEFAULT_CATALOG))
    parser.add_argument("--region", default="region50")
    parser.add_argument("--offset", type=int, default=0,
                         help="С какого по счёту документа (с текстовым слоем) начинать — для батчей")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true",
                         help="Скачать и распознать, но не индексировать в Qdrant")
    args = parser.parse_args()

    asyncio.run(run(args.catalog, args.region, args.offset, args.limit, args.dry_run))
