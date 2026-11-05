"""
Полная классификация документов региона pravo.gov.ru в файл-каталог:
одна строка CSV — один документ, с флагом "текстовый слой / скан",
датами и статусом наличия в нашей базе (Qdrant).

Зачем: разведка показала, что доля сканов резко растёт с годами
(Московская область, region50: 2018-2020 — 94-100% с текстовым слоем,
2025-2026 — 1-9%). Полный каталог нужен, чтобы при выборке документов
для ML-инженера не гадать по сэмплу, а брать точные данные по каждому
документу, и чтобы вести учёт того, что уже загружено в базу.

PDF скачивается и проверяется ТОЛЬКО через PyMuPDF (как в разведке) —
OCR здесь НЕ запускается, это разведка, а не индексация, OCR слишком
медленный для полного каталога. has_text_layer = False означает "это
скан", а не "документа не существует".

Формат вывода: CSV, человекочитаемый (Excel/pandas). Описание каждого
поля — см. словарь FIELD_DESCRIPTIONS ниже, при каждом запуске рядом
с CSV создаётся/обновляется файл <out>.schema.md с тем же описанием.

Устойчивость: пишет строки в CSV сразу по готовности (flush), а не
в конце — если скрипт прервётся на середине, уже собранные строки не
теряются. Если выходной файл уже существует, документы с уже
записанными doc_id пропускаются (докачка/продолжение, а не пересбор
с нуля).

Запуск:
    # Тестовый прогон — маленькая выборка
    python build_pravo_gov_catalog.py --region region50 --year 2024 --limit 50 --out test_small.csv

    # Прогон побольше
    python build_pravo_gov_catalog.py --region region50 --date-from 01.01.2018 --date-to 31.12.2026 --limit 500 --out test_big.csv

    # Полный каталог региона (без --limit — все документы за период)
    python build_pravo_gov_catalog.py --region region50 --date-from 01.01.2018 --date-to 31.12.2026 --out catalog_region50.csv
"""

import argparse
import asyncio
import csv
import html as html_mod
import os
import re
import sys
from datetime import datetime

if hasattr(sys.stdout, "buffer"):
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import aiohttp
import fitz  # PyMuPDF
from qdrant_client import QdrantClient

from index_pravo_gov import (
    BASE_URL, HEADERS, QDRANT_URL, COLLECTION_NAME, OCR_MIN_TEXT_LEN,
    fetch_page, fetch_bytes, clean_text, _parse_citation,
    ensure_payload_index, doc_exists,
)

# ── Описание полей каталога (единственный источник правды — отсюда же
# генерируется файл <out>.schema.md при каждом запуске) ────────────────────

FIELD_DESCRIPTIONS = {
    "doc_id": "Внутренний номер документа на pravo.gov.ru (eoNumber), 16 цифр. "
              "Уникален в рамках сайта, НЕ совместим с ID из RusLawOD.",
    "region": "Код блока/региона на pravo.gov.ru (например region50 = Московская область).",
    "doc_type": "Тип акта (Постановление/Распоряжение/Приказ/Указ/Решение/...), "
                "извлечён из заголовка документа.",
    "doc_number": "Официальный номер акта (например \"24РВ-72/1\"), как в заголовке.",
    "title": "Название документа — текст в кавычках из заголовка.",
    "signing_date": "Дата самого акта (\"от ДД.ММ.ГГГГ № ...\") — дата подписания/принятия, "
                    "парсится из заголовка документа.",
    "publish_date": "Дата опубликования документа на портале pravo.gov.ru (дата загрузки) — "
                    "отдельное поле метаданных карточки, не совпадает с датой подписания.",
    "registration_date": "Дата регистрации акта в Минюсте (\"Зарегистрирован ДД.ММ.ГГГГ\"), "
                          "если указана в заголовке. Есть не у всех документов — может быть пустой.",
    "dedup_key": "Ключ дедупликации (тип_акта|номер|дата_акта) — тот же, что использует "
                 "index_pravo_gov.py. Пустой, если номер или дата акта не распознались.",
    "file_size_kb": "Размер PDF в килобайтах, как указано в карточке на сайте.",
    "page_count": "Количество страниц PDF, как указано в карточке на сайте.",
    "has_text_layer": "True — PyMuPDF извлёк из PDF >= 100 символов текста (документ НЕ требует "
                       "OCR). False — текста почти нет, это скан. Пусто — PDF не скачался/не открылся "
                       "(см. download_error).",
    "download_error": "True, если PDF не удалось скачать или открыть как PDF. В этом случае "
                       "has_text_layer остаётся пустым — это не значит, что документ является сканом.",
    "in_database": "True/False — найден ли документ в нашей коллекции Qdrant (public_docs) "
                   "по dedup_key. Пусто, если dedup_key не распознан (сравнение не проводилось).",
    "checked_at": "Дата и время (ISO), когда эта строка каталога была собрана/проверена — "
                  "по нему видно, не устарела ли запись относительно текущего состояния сайта/базы.",
}

CSV_FIELDS = list(FIELD_DESCRIPTIONS.keys())

# Та же защита от троттлинга, что в index_pravo_gov.py: после нескольких
# подряд неудачных скачиваний PDF — длинная пауза, а не мгновенный переход
# к следующему документу.
CONSECUTIVE_FAILURES_THRESHOLD = 3
COOLDOWN_SECONDS = 90

ROW_SPLIT_RE = re.compile(r'<div class="documents-table-row">')


def write_schema_file(out_path: str):
    schema_path = os.path.splitext(out_path)[0] + ".schema.md"
    with open(schema_path, "w", encoding="utf-8") as f:
        f.write(f"# Описание полей каталога\n\n")
        f.write(f"Сгенерировано build_pravo_gov_catalog.py, {datetime.now().isoformat(timespec='seconds')}\n\n")
        for field in CSV_FIELDS:
            f.write(f"- **{field}** — {FIELD_DESCRIPTIONS[field]}\n")
    return schema_path


def parse_catalog_row(chunk: str, region: str) -> dict | None:
    """
    Извлекает все поля каталога из HTML-блока ОДНОГО документа со страницы
    листинга (между соседними <div class="documents-table-row">). В отличие
    от parse_listing_documents() в index_pravo_gov.py, достаёт не только
    реквизиты из заголовка, но и "Дата опубликования", размер PDF и число
    страниц — эти поля в листинге есть, но раньше не читались.
    """
    chunk = html_mod.unescape(chunk)

    id_m = re.search(r'href="/document/(\d{16})"', chunk)
    if not id_m:
        return None
    nd = id_m.group(1)

    name_m = re.search(r'class="documents-item-name">\s*(.*?)\s*</a>', chunk, re.DOTALL)
    header_html = name_m.group(1) if name_m else ""
    meta = _parse_citation(header_html, nd)

    header_text = clean_text(re.sub(r"<[^>]+>", " ", header_html))
    reg_m = re.search(r"Зарегистрирован[а]?\s+(\d{2}\.\d{2}\.\d{4})", header_text)
    registration_date = reg_m.group(1) if reg_m else ""

    pub_m = re.search(
        r'Дата опубликования:\s*</span>\s*<span class="info-data">\s*(\d{2}\.\d{2}\.\d{4})',
        chunk, re.DOTALL,
    )
    publish_date = pub_m.group(1) if pub_m else ""

    file_size_kb = ""
    size_m = re.search(r'documents-pdf-downloadlink">\s*([\d\s.,]+)\s*(Кб|КБ|Мб|МБ)', chunk)
    if size_m:
        raw = size_m.group(1).replace(" ", "").replace("\xa0", "").replace(",", ".")
        try:
            value = float(raw)
            if size_m.group(2).lower().startswith("м"):
                value *= 1024
            file_size_kb = round(value)
        except ValueError:
            file_size_kb = ""

    pages_m = re.search(r"\((\d+)\s*стр\.\)", chunk)
    page_count = pages_m.group(1) if pages_m else ""

    return {
        "doc_id": nd,
        "region": region,
        "doc_type": meta["doc_type"],
        "doc_number": meta["doc_number"],
        "title": meta["title"],
        "signing_date": meta["date"],
        "publish_date": publish_date,
        "registration_date": registration_date,
        "dedup_key": meta["dedup_key"] or "",
        "file_size_kb": file_size_kb,
        "page_count": page_count,
    }


async def fetch_listing_rows(session: aiohttp.ClientSession, region: str,
                              date_from: str, date_to: str, limit: int | None):
    """Собирает строки каталога (без PDF/базы) по всем страницам листинга."""
    rows = []
    index = 1
    while True:
        url = (f"{BASE_URL}/documents/block/{region}?pageSize=30&index={index}"
               f"&DocumentDateFrom={date_from}&DocumentDateTo={date_to}")
        html = await fetch_page(session, url)
        if not html:
            break
        chunks = ROW_SPLIT_RE.split(html)[1:]
        if not chunks:
            break
        got_any = False
        for chunk in chunks:
            row = parse_catalog_row(chunk, region)
            if row:
                rows.append(row)
                got_any = True
                if limit is not None and len(rows) >= limit:
                    return rows
        if not got_any:
            break
        index += 1
        await asyncio.sleep(0.3)
    return rows


async def classify_and_write(session: aiohttp.ClientSession, client: QdrantClient,
                              rows: list[dict], writer: csv.DictWriter, f, skip_ids: set):
    consecutive_failures = 0
    written = 0
    for row in rows:
        nd = row["doc_id"]
        if nd in skip_ids:
            continue

        if consecutive_failures >= CONSECUTIVE_FAILURES_THRESHOLD:
            print(f"    {consecutive_failures} сбоев подряд — пауза {COOLDOWN_SECONDS}с")
            await asyncio.sleep(COOLDOWN_SECONDS)
            consecutive_failures = 0

        pdf_bytes = await fetch_bytes(session, f"{BASE_URL}/file/pdf?eoNumber={nd}")
        has_text_layer = ""
        download_error = False
        if not pdf_bytes:
            download_error = True
            consecutive_failures += 1
        else:
            consecutive_failures = 0
            try:
                doc = fitz.open(stream=pdf_bytes, filetype="pdf")
                text = "".join(page.get_text() for page in doc)
                has_text_layer = len(text) >= OCR_MIN_TEXT_LEN
            except Exception:
                download_error = True

        in_database = doc_exists(client, row["dedup_key"]) if row["dedup_key"] else ""

        record = dict(row)
        record.update({
            "has_text_layer": has_text_layer,
            "download_error": download_error,
            "in_database": in_database,
            "checked_at": datetime.now().isoformat(timespec="seconds"),
        })
        writer.writerow(record)
        f.flush()
        written += 1
        if written % 20 == 0:
            print(f"    обработано {written}/{len(rows) - len(skip_ids)}")
        await asyncio.sleep(0.2)
    return written


async def main(region: str, date_from: str, date_to: str, limit: int | None, out: str):
    print("=== LegalSearch: каталог документов pravo.gov.ru ===\n")
    print(f"Регион: {region}")
    print(f"Период: {date_from} — {date_to}")
    print(f"Лимит:  {limit if limit else 'без ограничения'}")
    print(f"Файл:   {out}\n")

    skip_ids = set()
    file_exists = os.path.exists(out)
    if file_exists:
        with open(out, "r", encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                skip_ids.add(r["doc_id"])
        print(f"Файл уже существует, в нём {len(skip_ids)} документов — они будут пропущены (докачка).\n")

    print("Подключаюсь к Qdrant...")
    client = QdrantClient(url=QDRANT_URL, timeout=60)
    ensure_payload_index(client)

    async with aiohttp.ClientSession() as session:
        print("Собираю листинг документов...")
        rows = await fetch_listing_rows(session, region, date_from, date_to, limit)
        print(f"Найдено в листинге: {len(rows)}\n")

        mode = "a" if file_exists else "w"
        with open(out, mode, encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            if not file_exists:
                writer.writeheader()
                f.flush()
            print("Скачиваю и классифицирую PDF...")
            written = await classify_and_write(session, client, rows, writer, f, skip_ids)

    schema_path = write_schema_file(out)
    print(f"\n✓ Готово! Записано новых строк: {written}")
    print(f"  Каталог:         {out}")
    print(f"  Описание полей:  {schema_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--region", default="region50", help="Код блока/региона (region50 = Московская область)")
    parser.add_argument("--date-from", default="01.01.2018")
    parser.add_argument("--date-to", default=datetime.now().strftime("%d.%m.%Y"))
    parser.add_argument("--year", type=int, default=None,
                         help="Сокращение: взять --date-from/--date-to как весь этот год")
    parser.add_argument("--limit", type=int, default=None,
                         help="Максимум документов в этом прогоне (для тестовых выборок)")
    parser.add_argument("--out", required=True, help="Путь к выходному CSV")
    args = parser.parse_args()

    date_from, date_to = args.date_from, args.date_to
    if args.year is not None:
        date_from = f"01.01.{args.year}"
        date_to = f"31.12.{args.year}"

    asyncio.run(main(args.region, date_from, date_to, args.limit, args.out))
