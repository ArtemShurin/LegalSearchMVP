"""
Индексация ОСТАЛЬНОГО пула publication.pravo.gov.ru, за пределами 4 основных
федеральных блоков (которые уже проиндексированы/слинкованы ранее — см.
project_pravo_gov_url_linking.md). В первую очередь это блок `subjects`
(региональное законодательство субъектов РФ) — 92% всего сайта (1.47М
документов), которого в текущем корпусе LegalSearch почти наверняка нет
вообще (RusLawOD — только федеральные НПА). См. разведку масштаба в памяти
project_pravo_gov_scale_discovery.md.

Это НЕ только линковка (как backfill_pravo_gov_url_ids.py) — для документов,
которых у нас нет, делается полная индексация: PDF → текст (OCR при
необходимости) → чанки → эмбеддинг → Qdrant. index_pravo_gov.py переиспользуется
(process_documents, _parse_citation) — внесены 2 небольших обратно совместимых
правки (поле authority_text в _parse_citation, проброс block_*_for_laws полей
в index_batch/process_documents), старые вызовы (4 федеральных блока,
backfill_pravo_gov_url_ids.py) эти поля просто не используют и не затронуты.

Ключевая оптимизация (идея пользователя 2026-10-08): прежде чем качать PDF,
исключаем документы, dedup_key которых УЖЕ есть в Qdrant — загружаем ВСЕ
существующие dedup_key ОДНИМ проходом в память в начале, а не проверяем
через сеть по одному документу (доходит до 1.47М документов — поштучные
Qdrant-запросы в цикле неприемлемо дороги, тот же урок, что уже был записан
в project_pravo_gov_dedup.md про filter-based SetPayload на больших
коллекциях).

Тип/название органа-издателя (block_type_for_laws/block_name_for_laws/
block_code_for_laws) определяются ПО КАЖДОМУ ДОКУМЕНТУ ОТДЕЛЬНО из текста его
реквизитов (classify_authority()), а не наследуются от --block — поэтому
корректно работает и через агрегаты (subjects/federal_authorities), и через
отдельные коды (regionNN/foivXXX), с одинаковой точностью на уровне документа.

ВАЖНО О МАСШТАБЕ: блок subjects — 1 472 097 документов (92% сайта). Полная
индексация такого объёма — это НЕ прогон на час, а дни-недели машинного
времени (PDF-скачивание + OCR + эмбеддинг на GPU для потенциально сотен
тысяч НОВЫХ документов, после вычета дублей). Всегда начинать с --limit на
малом числе и/или dry-run, прежде чем запускать без ограничений.

Запуск:
    # Пилот — 100 документов региона, dry-run:
    python index_pravo_gov_remaining.py --block subjects --date-from 01.01.2024 --date-to 01.01.2026 --limit 100 --dry-run

    # Какой блок обрабатывать — любой валидный на сайте (не только 4 известных):
    python index_pravo_gov_remaining.py --block subjects ...
"""

import argparse
import asyncio
import io
import re
import sys
import time
from datetime import datetime, timedelta

if hasattr(sys.stdout, 'buffer'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

import aiohttp
from tqdm import tqdm
from qdrant_client import QdrantClient

from index_pravo_gov import (
    fetch_page, parse_total_count, parse_listing_documents, process_documents,
    ensure_payload_index, QDRANT_URL, BASE_URL, PAGE_SIZE, EMBEDDING_MODEL,
)

COOLDOWN_SECONDS = 90  # та же пауза, что и в index_pravo_gov.py, для согласованности

# Известные именованные (не агрегатные, не foiv/region) блоки — используются
# только как block_code_for_laws (traceability, с какого крауглинга пришёл
# документ) — см. разведку в project_pravo_gov_scale_discovery.md.
KNOWN_TOP_BLOCKS = {
    "president": "Президент РФ",
    "government": "Правительство РФ",
    "council_1": "Совет Федерации",
    "council_2": "Государственная Дума",
    "court": "Суды",
    "international": "Международные акты",
    "un_securitycouncil": "Совбез ООН",
}

# Та же логика, что уже проверена при построении pravo_gov_block_names.json
# (см. scratchpad build_block_names.py) — если в тексте органа-издателя есть
# маркер региона (Республика/область/край/автономный округ/Москва/СПб/
# Севастополь), документ региональный, иначе — федеральный орган/ведомство.
REGION_MARKER = re.compile(
    r'('
    r'Республик[аи]\s+[А-ЯЁ][\w\-\(\) ]*?(?=[,.]|$)'
    r'|[А-ЯЁ][а-яёА-ЯЁ\-]*(?:ой|ей)\s+Республики'
    r'|[А-ЯЁ][а-яёА-ЯЁ\-]*(?:ого|его)\s+кра[яею]'
    r'|[А-ЯЁ][а-яёА-ЯЁ\-]*(?:ой|ей)\s+област[ьи]'
    r'|[А-ЯЁ][а-яёА-ЯЁ\-]*(?:ого|его)\s+автономн\w+\s+округ\w*'
    r'|автономн\w+\s+округ\w*'
    r'|Еврейской\s+автономной\s+области'
    r'|Москв[а-яё]*|Санкт-Петербург[а-яё]*|Севастопол[а-яё]*'
    r')(?=[,.\s]|$)',
)


def classify_authority(authority_text: str, crawl_block: str) -> tuple[str, str | None, str]:
    """
    Возвращает (block_type_for_laws, block_name_for_laws, block_code_for_laws)
    ПО КАЖДОМУ ДОКУМЕНТУ ОТДЕЛЬНО — разбирает текст органа-издателя этого
    конкретного документа (authority_text из _parse_citation(), см. правку
    в index_pravo_gov.py), а не наследует тип/название от того, через какой
    --block шёл краулинг. Работает корректно и для агрегатов (subjects,
    federal_authorities), и для отдельных кодов — точность на уровне
    документа, не на уровне всего прогона (идея пользователя 2026-10-08).
    """
    if crawl_block in KNOWN_TOP_BLOCKS:
        return crawl_block, KNOWN_TOP_BLOCKS[crawl_block], crawl_block

    m = REGION_MARKER.search(authority_text)
    if m:
        return "region", m.group(1).strip(), crawl_block

    # Нет маркера региона — федеральный орган/ведомство, имя = текст органа
    # без ведущего слова типа акта (Приказ/Постановление/...)
    name = re.sub(
        r'^(Федеральный\s+конституционный\s+закон|Федеральный\s+закон|Указ|'
        r'Постановление|Распоряжение|Приказ|Решение)\s+',
        '', authority_text, flags=re.IGNORECASE,
    ).strip()
    return "ministry", (name or None), crawl_block


def load_all_dedup_keys(client: QdrantClient, collection: str = "public_docs") -> set[str]:
    """
    Один проход по ВСЕЙ коллекции — собирает множество уже существующих
    dedup_key в память. Делается ОДИН РАЗ в начале, а не по запросу на
    документ — иначе на объёме в сотни тысяч-миллионы документов поштучные
    Qdrant-запросы станут узким местом (см. docstring модуля).
    """
    keys = set()
    offset = None
    pbar = tqdm(desc="Загружаю существующие dedup_key из Qdrant", unit="точка")
    while True:
        results, offset = client.scroll(
            collection_name=collection,
            limit=1000,
            offset=offset,
            with_payload=["dedup_key"],
            with_vectors=False,
        )
        for p in results:
            k = p.payload.get("dedup_key")
            if k:
                keys.add(k)
        pbar.update(len(results))
        if offset is None:
            break
    pbar.close()
    return keys


async def collect_block_documents(
    session: aiohttp.ClientSession,
    block: str,
    date_from: str,
    date_to: str,
) -> list[dict]:
    """
    Обобщённая версия collect_documents() из index_pravo_gov.py — работает
    для ЛЮБОГО значения block (не только 4 известных федеральных), через
    URL-паттерн /documents/block/{block}, который реально поддерживает сайт
    (проверено эмпирически для 'subjects', 'court', 'international',
    'un_securitycouncil' — см. project_pravo_gov_scale_discovery.md).
    Та же защита от троттлинга (пауза при пустых страницах), что в оригинале.
    """
    all_docs = []
    seen = set()
    index = 1
    total = None
    page_retries = 0
    pbar = tqdm(desc=f"  Страницы [{block}]", unit="стр", leave=False)

    while True:
        url = (
            f"{BASE_URL}/documents/block/{block}"
            f"?pageSize={PAGE_SIZE}&index={index}"
            f"&DocumentDateFrom={date_from}&DocumentDateTo={date_to}"
        )
        html = await fetch_page(session, url)
        if not html:
            if total and len(all_docs) < total and page_retries < 3:
                page_retries += 1
                tqdm.write(f"    Пустая страница {index}, пауза {COOLDOWN_SECONDS}с (попытка {page_retries}/3)")
                await asyncio.sleep(COOLDOWN_SECONDS)
                continue
            break
        page_retries = 0

        if total is None:
            total = parse_total_count(html)

        docs = parse_listing_documents(html)
        if not docs:
            break

        new_docs = [d for d in docs if d["doc_id"] not in seen]
        seen.update(d["doc_id"] for d in new_docs)
        all_docs.extend(new_docs)
        pbar.set_postfix(найдено=len(all_docs), всего=total or "?")
        pbar.update(1)

        if total and len(all_docs) >= total:
            break
        if not new_docs:
            break
        index += 1
        await asyncio.sleep(0.3)

    pbar.close()
    return all_docs


async def main(block: str, date_from: str, date_to: str, limit: int | None, dry_run: bool):
    print("=== Индексация остального пула pravo.gov.ru ===\n")
    print(f"Блок:   {block}")
    print(f"Период: {date_from} — {date_to}")
    print(f"Лимит:  {limit if limit else 'без ограничения (ОСТОРОЖНО — проверьте масштаб блока заранее)'}")
    print(f"Режим:  {'dry-run (без индексации)' if dry_run else 'полная индексация'}\n")

    client = QdrantClient(url=QDRANT_URL, timeout=60)
    if not dry_run:
        ensure_payload_index(client)

    existing_keys = load_all_dedup_keys(client)
    print(f"Уже в Qdrant: {len(existing_keys):,} уникальных документов (по dedup_key)\n")

    model = None
    if not dry_run:
        print(f"Загружаю модель {EMBEDDING_MODEL}...")
        from sentence_transformers import SentenceTransformer
        model = SentenceTransformer(EMBEDDING_MODEL, device="cuda")

    async with aiohttp.ClientSession() as session:
        print(f"Собираю листинг блока '{block}'...")
        docs = await collect_block_documents(session, block, date_from, date_to)
        print(f"  Собрано со страниц листинга: {len(docs):,}")

        new_docs = [d for d in docs if d["dedup_key"] not in existing_keys]
        print(f"  Из них НОВЫХ (нет в Qdrant): {len(new_docs):,}")
        print(f"  Уже есть, пропущены сразу (без похода за PDF): {len(docs) - len(new_docs):,}\n")

        # Тип/название органа — ПО КАЖДОМУ документу отдельно, из его же
        # реквизитов (не наследуется от --block) — см. classify_authority().
        type_counts = {}
        for d in new_docs:
            block_type, block_name, block_code = classify_authority(
                d.get("authority_text", ""), block)
            d["block_type_for_laws"] = block_type
            d["block_name_for_laws"] = block_name
            d["block_code_for_laws"] = block_code
            type_counts[block_type] = type_counts.get(block_type, 0) + 1
        if type_counts:
            print(f"  Классификация по типу: {type_counts}\n")

        stats = {"processed": 0, "indexed": 0, "skipped_dup": 0, "skipped_no_pdf": 0,
                  "skipped_empty": 0, "chunks": 0, "ocr_used": 0, "text_layer": 0}

        if new_docs:
            await process_documents(
                session, client, model, new_docs, stats, limit=limit, dry_run=dry_run,
            )

    print(f"\n{'='*60}")
    print("ИТОГИ")
    print(f"  На листинге всего:                  {len(docs):,}")
    print(f"  Уже было в Qdrant (пропущено сразу): {len(docs) - len(new_docs):,}")
    print(f"  Новых обработано:                    {stats['processed']:,}")
    print(f"  Проиндексировано:                    {stats['indexed']:,}")
    print(f"  Пропущено (дубли, доп. проверка):    {stats['skipped_dup']:,}")
    print(f"  Пропущено (нет PDF):                 {stats['skipped_no_pdf']:,}")
    print(f"  Пропущено (пустой текст):            {stats['skipped_empty']:,}")
    print(f"  Загружено чанков:                    {stats['chunks']:,}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--block", default="subjects",
                         help="Блок сайта (subjects — региональное право, по умолчанию; "
                              "также доступны court, international, un_securitycouncil и др.)")
    default_to = datetime.now().strftime("%d.%m.%Y")
    default_from = "01.01.1990"
    parser.add_argument("--date-from", default=default_from, help="ДД.ММ.ГГГГ")
    parser.add_argument("--date-to", default=default_to, help="ДД.ММ.ГГГГ")
    parser.add_argument("--limit", type=int, default=None,
                         help="Максимум НОВЫХ документов для обработки (рекомендуется "
                              "всегда задавать явно, учитывая масштаб блока subjects)")
    parser.add_argument("--dry-run", action="store_true",
                         help="Скачать и распознать, но не индексировать в Qdrant")
    args = parser.parse_args()

    asyncio.run(main(args.block, args.date_from, args.date_to, args.limit, args.dry_run))
