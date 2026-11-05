"""
Целевая выборка регионального права pravo.gov.ru для ML-инженера — 2000
документов, равномерно по 25 репрезентативным регионам (по ~3 на каждый из
8 федеральных округов) и 10 последним годам (2017-2026), по 8 документов на
ячейку регион×год. Не полная индексация блока subjects (1.47М документов,
см. index_pravo_gov_remaining.py) — целевой диверсифицированный пилот под
конкретный запрос: "региональные акты за разные годы + метаданные (регион,
даты)" (см. memory: project-ml-package-refactor, project-dev-hypotheses-vkr).

Регионы выбраны по коду субъекта РФ (стандартная нумерация, как на
автономерах/ИНН) — коды и реальные названия см. REGION_NAMES ниже. Выбор:
3-4 на федеральный округ, предпочтение регионам с бОльшим объёмом документов
на сайте (см. scripts/pravo_gov_block_names.json) — чтобы уложиться в целевые
80 документов/регион с запасом. Исключены коды с total=0 в последней разведке
(Москва region77, новые территории region84/85 — see project-pravo-gov-scale-discovery).

Переиспользует process_documents/classify_authority/load_all_dedup_keys —
логика скачивания PDF, OCR, чанкинга, эмбеддинга и дедупликации идентична
index_pravo_gov_remaining.py, здесь только оркестрация по сетке регион×год
с ранней остановкой на ячейку (не тянем весь листинг региона/года, только
пока не наберём целевое число НОВЫХ документов).

Проверка дубликатов: существующие dedup_key грузятся из Qdrant ОДИН раз в
начале каждого запуска (load_all_dedup_keys) — при раздельных запусках частей
каждая часть подхватывает актуальное состояние Qdrant, написанное предыдущими
частями. --part N/4 делит 25 регионов на 4 НЕПЕРЕСЕКАЮЩИХСЯ группы (разные
регионы в разных частях) — поэтому части физически не могут выбрать один и
тот же документ, и их можно запускать как последовательно, так и параллельно
(хотя параллельно вчетвером упрутся в одну GPU для OCR/эмбеддинга — смысла
мало, см. project-pravo-gov-regional-sample).

Запуск:
    # Пилот — по 1 региону и все года, посмотреть что происходит:
    python scripts/index_pravo_gov_regional_sample.py --pilot --dry-run

    # Боевой прогон по частям (500 документов каждая, 4 части = 2000 всего):
    python scripts/index_pravo_gov_regional_sample.py --part 1/4
    python scripts/index_pravo_gov_regional_sample.py --part 2/4
    python scripts/index_pravo_gov_regional_sample.py --part 3/4
    python scripts/index_pravo_gov_regional_sample.py --part 4/4

    # Весь план целиком, без деления на части:
    python scripts/index_pravo_gov_regional_sample.py
"""

import argparse
import asyncio
import io
import sys
from datetime import datetime

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
from index_pravo_gov_remaining import classify_authority, load_all_dedup_keys

COOLDOWN_SECONDS = 90
TARGET_PER_REGION = 80   # 2000 / 25 регионов
YEARS = list(range(2017, 2027))  # 10 лет, по 8 документов/год при ровном делении

# 25 регионов, по 3-4 на федеральный округ — код субъекта РФ: название.
REGION_NAMES = {
    # ЦФО
    "region50": "Московская область",
    "region36": "Воронежская область",
    "region69": "Тверская область",
    "region71": "Тульская область",
    # СЗФО
    "region78": "Санкт-Петербург",
    "region29": "Архангельская область",
    "region47": "Ленинградская область",
    # ЮФО
    "region23": "Краснодарский край",
    "region34": "Волгоградская область",
    "region61": "Ростовская область",
    "region92": "Севастополь",
    # СКФО
    "region05": "Республика Дагестан",
    "region20": "Чеченская Республика",
    # ПФО
    "region02": "Республика Башкортостан",
    "region52": "Нижегородская область",
    "region63": "Самарская область",
    # УФО
    "region66": "Свердловская область",
    "region74": "Челябинская область",
    "region89": "Ямало-Ненецкий АО",
    # СФО
    "region38": "Иркутская область",
    "region54": "Новосибирская область",
    "region55": "Омская область",
    # ДФО
    "region25": "Приморский край",
    "region41": "Камчатский край",
    "region87": "Чукотский АО",
}

TOTAL_TARGET = 2000  # TARGET_PER_REGION * len(REGION_NAMES)


def split_into_parts(regions: dict[str, str], part: tuple[int, int]) -> dict[str, str]:
    """Делит регионы на M непересекающихся групп (сохраняя порядок/группировку
    по федеральным округам), возвращает регионы части N (0-индексация)."""
    index, total = part
    items = list(regions.items())
    n = len(items)
    # Делим максимально ровно: первые (n % total) частей получают на 1 регион больше.
    base, extra = divmod(n, total)
    sizes = [base + 1 if i < extra else base for i in range(total)]
    start = sum(sizes[:index])
    end = start + sizes[index]
    return dict(items[start:end])


async def collect_cell_candidates(
    session: aiohttp.ClientSession,
    block: str,
    date_from: str,
    date_to: str,
    existing_keys: set[str],
    seen_this_run: set[str],
    target: int,
) -> list[dict]:
    """
    Паджинирует листинг блока за указанный период, останавливаясь, как только
    набрано `target` НОВЫХ (не в Qdrant и не встреченных раньше в этом же
    прогоне) документов — либо когда листинг исчерпан. В отличие от
    collect_block_documents() в index_pravo_gov_remaining.py (тянет листинг
    целиком), здесь цель — всего 8 документов на ячейку, тянуть весь объём
    региона/года (сотни-тысячи) ради этого не нужно.
    """
    candidates = []
    index = 1
    page_retries = 0

    while len(candidates) < target:
        url = (
            f"{BASE_URL}/documents/block/{block}"
            f"?pageSize={PAGE_SIZE}&index={index}"
            f"&DocumentDateFrom={date_from}&DocumentDateTo={date_to}"
        )
        html = await fetch_page(session, url)
        if not html:
            if page_retries < 2:
                page_retries += 1
                await asyncio.sleep(COOLDOWN_SECONDS)
                continue
            break
        page_retries = 0

        docs = parse_listing_documents(html)
        if not docs:
            break

        for d in docs:
            key = d["dedup_key"]
            if key in existing_keys or key in seen_this_run:
                continue
            seen_this_run.add(key)
            candidates.append(d)
            if len(candidates) >= target:
                break

        index += 1
        await asyncio.sleep(0.3)

    return candidates


async def main(dry_run: bool, pilot: bool, part: tuple[int, int] | None):
    regions = REGION_NAMES
    years = YEARS
    part_target = TOTAL_TARGET

    if part is not None:
        index, total = part
        regions = split_into_parts(REGION_NAMES, part)
        part_target = -(-TOTAL_TARGET // total)  # ceil(2000/total), напр. 500 при total=4

    if pilot:
        regions = dict(list(regions.items())[:1])
        part_target = 8
        # Все годы, не только первый — ранние года могут давать 0 (архив
        # региона стартует позже, не баг, см. project-pravo-gov-regional-sample),
        # перенос недобора на следующие годы нужно проверить end-to-end.

    print("=== Региональная выборка pravo.gov.ru для ML-инженера ===\n")
    if part is not None:
        print(f"Часть:           {part[0]+1}/{part[1]}")
    print(f"Регионов:        {len(regions)}")
    print(f"Лет:             {years[0]}-{years[-1]} ({len(years)})")
    print(f"Цель (эта часть): {part_target} документов")
    print(f"Режим:           {'ПИЛОТ (1 регион x все года)' if pilot else 'боевой'}, "
          f"{'dry-run (без индексации)' if dry_run else 'полная индексация'}\n")

    client = QdrantClient(url=QDRANT_URL, timeout=60)
    if not dry_run:
        ensure_payload_index(client)

    existing_keys = load_all_dedup_keys(client)
    print(f"Уже в Qdrant: {len(existing_keys):,} уникальных документов (по dedup_key)\n")
    seen_this_run: set[str] = set()

    model = None
    if not dry_run:
        print(f"Загружаю модель {EMBEDDING_MODEL}...")
        from sentence_transformers import SentenceTransformer
        model = SentenceTransformer(EMBEDDING_MODEL, device="cuda")

    global_stats = {"processed": 0, "indexed": 0, "skipped_dup": 0, "skipped_no_pdf": 0,
                     "skipped_empty": 0, "chunks": 0, "ocr_used": 0, "text_layer": 0}
    per_region_indexed: dict[str, int] = {code: 0 for code in regions}
    per_region_target_used: dict[str, int] = {}

    remaining_part = part_target

    async with aiohttp.ClientSession() as session:
        region_bar = tqdm(regions.items(), desc="Регионы", unit="регион")
        for ri, (block, region_name) in enumerate(region_bar):
            region_bar.set_postfix(регион=region_name)
            regions_left = len(regions) - ri
            # Перенос недобора между РЕГИОНАМИ внутри части — та же логика,
            # что уже проверена между годами (см. below): если предыдущий
            # регион недодал (мало документов на сайте в выбранных годах),
            # следующие регионы в этой части компенсируют остаток.
            per_region_target = max(0, -(-remaining_part // regions_left)) if not pilot else 8
            remaining = per_region_target
            per_region_target_used[block] = per_region_target

            for i, year in enumerate(years):
                years_left = len(years) - i
                if years_left <= 0:
                    break
                per_year_target = max(0, -(-remaining // years_left))  # ceil(remaining/years_left)
                if per_year_target == 0:
                    continue

                date_from = f"01.01.{year}"
                date_to = f"31.12.{year}"

                candidates = await collect_cell_candidates(
                    session, block, date_from, date_to,
                    existing_keys, seen_this_run, per_year_target,
                )

                if candidates:
                    for d in candidates:
                        block_type, block_name, block_code = classify_authority(
                            d.get("authority_text", ""), block)
                        d["block_type_for_laws"] = block_type
                        d["block_name_for_laws"] = block_name
                        d["block_code_for_laws"] = block_code

                    cell_stats = {"processed": 0, "indexed": 0, "skipped_dup": 0,
                                   "skipped_no_pdf": 0, "skipped_empty": 0, "chunks": 0,
                                   "ocr_used": 0, "text_layer": 0}
                    await process_documents(
                        session, client, model, candidates, cell_stats,
                        limit=len(candidates), dry_run=dry_run,
                    )
                    for k in global_stats:
                        global_stats[k] += cell_stats[k]
                    per_region_indexed[block] += cell_stats["indexed"]
                    remaining -= cell_stats["indexed"]

                tqdm.write(f"  [{region_name} / {year}] найдено кандидатов: {len(candidates)}, "
                           f"проиндексировано: {cell_stats['indexed'] if candidates else 0}, "
                           f"остаток цели региона: {max(0, remaining)}")

            remaining_part -= per_region_target - max(0, remaining)

    print(f"\n{'='*60}")
    print("ИТОГИ")
    print(f"  Новых обработано:                    {global_stats['processed']:,}")
    print(f"  Проиндексировано:                    {global_stats['indexed']:,}")
    print(f"  Пропущено (дубли, доп. проверка):    {global_stats['skipped_dup']:,}")
    print(f"  Пропущено (нет PDF):                 {global_stats['skipped_no_pdf']:,}")
    print(f"  Пропущено (пустой текст):            {global_stats['skipped_empty']:,}")
    print(f"  Загружено чанков:                    {global_stats['chunks']:,}")
    print(f"  Цель этой части:                     {part_target} (факт: {global_stats['indexed']:,})")
    print(f"\n  По регионам (цель — динамическая, с переносом недобора):")
    for block, count in per_region_indexed.items():
        target = per_region_target_used.get(block, 0)
        mark = "⚠" if count < target else "✓"
        print(f"    {mark} {REGION_NAMES[block]:35s} ({block}): {count}/{target}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Региональная выборка pravo.gov.ru (2000 документов, 25 регионов x 10 лет)")
    parser.add_argument("--dry-run", action="store_true",
                         help="Скачать и распознать, но не индексировать в Qdrant")
    parser.add_argument("--pilot", action="store_true",
                         help="Пилот: только 1 регион x все года x 8 документов, для проверки перед боевым прогоном")
    parser.add_argument("--part", default=None,
                         help="N/M — делит 25 регионов на M непересекающихся групп (разные регионы "
                              "в разных частях, безопасно для дублей), обрабатывает часть N (1..M), "
                              "цель части = ceil(2000/M). Например --part 1/4 .. --part 4/4")
    args = parser.parse_args()

    part = None
    if args.part:
        idx_str, total_str = args.part.split("/")
        part = (int(idx_str) - 1, int(total_str))  # 1-индексация снаружи -> 0-индексация внутри

    asyncio.run(main(args.dry_run, args.pilot, part))
