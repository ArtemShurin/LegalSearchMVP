"""
Парсинг и индексация федеральных НПА с publication.pravo.gov.ru.

Стратегия:
  1. Итерируемся по блокам федеральных органов власти
  2. Для каждого документа скачиваем PDF
  3. Извлекаем текст через PyMuPDF
  4. Дедуплицируем по Nd против уже проиндексированных данных
  5. Индексируем в Qdrant

Запуск:
    python index_pravo_gov.py [--date-from YYYY-MM-DD] [--date-to YYYY-MM-DD]

По умолчанию: документы за последние 2 года.
"""

import argparse
import asyncio
import io
import re
import sys
import uuid
import time
from datetime import datetime, timedelta

if hasattr(sys.stdout, 'buffer'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

import aiohttp
import fitz  # PyMuPDF
from tqdm import tqdm
import easyocr

from dedup_key import make_dedup_key
from sentence_transformers import SentenceTransformer
from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct, Filter, FieldCondition, MatchValue

# ── Константы ──────────────────────────────────────────────────────────────

QDRANT_URL = "http://localhost:6333"
COLLECTION_NAME = "public_docs"
EMBEDDING_MODEL = "intfloat/multilingual-e5-large"

CHUNK_SIZE = 400
CHUNK_OVERLAP = 50
BATCH_SIZE = 64

BASE_URL = "http://publication.pravo.gov.ru"
PAGE_SIZE = 50  # документов за один запрос к поиску

# Федеральные блоки (только НПА высшего уровня)
FEDERAL_BLOCKS = [
    ("president",   "Президент РФ"),
    ("government",  "Правительство РФ"),
    ("council_1",   "Совет Федерации"),
    ("council_2",   "Государственная Дума"),
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml",
}

# ── Текстовые утилиты ───────────────────────────────────────────────────────

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


def clean_text(text: str) -> str:
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


OCR_MIN_TEXT_LEN = 100  # если PyMuPDF вернул меньше символов — считаем что текстового слоя нет

_ocr_reader = None


def _get_ocr_reader():
    """Ленивая загрузка EasyOCR (Reader создаётся один раз и держится в памяти)."""
    global _ocr_reader
    if _ocr_reader is None:
        print("  Загружаю EasyOCR (GPU)...")
        _ocr_reader = easyocr.Reader(["ru", "en"], gpu=True)
    return _ocr_reader


def extract_via_pymupdf(pdf_bytes: bytes) -> str:
    """Извлекает текст из PDF с текстовым слоем через PyMuPDF (быстро)."""
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        pages = [page.get_text() for page in doc]
        return clean_text("\n".join(pages))
    except Exception:
        return ""


def extract_via_easyocr(pdf_bytes: bytes) -> str:
    """Рендерит страницы PDF в изображения и распознаёт текст через EasyOCR (медленно)."""
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        reader = _get_ocr_reader()
        pages_text = []
        for page in doc:
            pix = page.get_pixmap(dpi=200)
            img_bytes = pix.tobytes("png")
            results = reader.readtext(img_bytes, detail=0, paragraph=True)
            pages_text.append("\n".join(results))
        return clean_text("\n".join(pages_text))
    except Exception:
        return ""


def extract_pdf_text(pdf_bytes: bytes) -> tuple[str, bool]:
    """
    Гибридное извлечение: сначала PyMuPDF (текстовый слой), и только если
    текста почти нет — OCR через EasyOCR (картиночный PDF).
    Возвращает (текст, использовался_ли_OCR).
    """
    text = extract_via_pymupdf(pdf_bytes)
    if len(text) >= OCR_MIN_TEXT_LEN:
        return text, False
    return extract_via_easyocr(pdf_bytes), True


# ── Qdrant ─────────────────────────────────────────────────────────────────

def ensure_payload_index(client: QdrantClient):
    """Создаёт индексы на doc_id и dedup_key для быстрого поиска дублей."""
    from qdrant_client.models import PayloadSchemaType
    for field in ("doc_id", "dedup_key"):
        try:
            client.create_payload_index(
                collection_name=COLLECTION_NAME,
                field_name=field,
                field_schema=PayloadSchemaType.KEYWORD,
            )
            print(f"Индекс по {field} создан.")
        except Exception:
            pass  # Уже существует


def doc_exists(client: QdrantClient, dedup_key: str | None) -> bool:
    """
    Проверяет наличие документа в Qdrant по ключу дедупликации (тип+номер+дата акта).
    ВАЖНО: не по внутреннему doc_id — ID pravo.gov.ru несовместим с pravogovruNd
    из RusLawOD (разные системы нумерации), см. dedup_key.py.
    """
    if not dedup_key:
        return False  # не смогли распарсить реквизиты — не блокируем индексацию
    results, _ = client.scroll(
        collection_name=COLLECTION_NAME,
        scroll_filter=Filter(
            must=[FieldCondition(key="dedup_key", match=MatchValue(value=dedup_key))]
        ),
        limit=1,
        with_payload=False,
        with_vectors=False,
        timeout=30,
    )
    return len(results) > 0


# Опциональные поля классификации органа-издателя (только для документов,
# проиндексированных через index_pravo_gov_remaining.py — region/foiv блоки).
# Передаются через meta/batch-словарь только если присутствуют, старые вызовы
# (4 федеральных блока, backfill_pravo_gov_url_ids.py) их не ставят вообще —
# не ломает обратную совместимость, index_pravo_gov.py больше нигде не меняется.
_BLOCK_META_KEYS = ("block_type_for_laws", "block_name_for_laws", "block_code_for_laws")


def index_batch(client: QdrantClient, model: SentenceTransformer, batch: list[dict]):
    texts = [f"passage: {item['text']}" for item in batch]
    embeddings = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
    points = []
    for i, item in enumerate(batch):
        payload = {
            "doc_id":    item["doc_id"],
            "title":     item["title"],
            "text":      item["text"],
            "doc_type":  item["doc_type"],
            "date":      item["date"],
            "chunk_idx": item["chunk_idx"],
            "source":    "pravo.gov.ru",
            "dedup_key": item["dedup_key"],
        }
        for key in _BLOCK_META_KEYS:
            if item.get(key):
                payload[key] = item[key]
        points.append(PointStruct(id=str(uuid.uuid4()), vector=embeddings[i].tolist(), payload=payload))
    client.upsert(collection_name=COLLECTION_NAME, points=points)


# ── HTTP-утилиты ────────────────────────────────────────────────────────────

def parse_total_count(html: str) -> int:
    """
    Извлекает общее число найденных документов из блока
    <div class="page-navigation-count">Найдено записей: с 1 по 30 из 2517</div>.
    """
    m = re.search(r'из\s*([\d\s\xa0]+)\s*</div>', html)
    if not m:
        return 0
    return int(re.sub(r'\s', '', m.group(1)))


def _parse_citation(header_html: str, nd: str) -> dict:
    """
    Разбирает блок реквизитов вида
      "{Тип} {Издатель} от {дата} № {номер}<br /> "{заголовок}""
    в структуру с готовым dedup_key. Общий парсер и для страницы листинга,
    и для страницы отдельного документа — формат идентичен на обеих.
    """
    header_text = clean_text(re.sub(r'<[^>]+>', ' ', header_html))

    date_num = re.search(r'от\s+(\d{2}\.\d{2}\.\d{4})\s*№\s*([^\s"]+)', header_text)
    date = date_num.group(1) if date_num else ""
    number = date_num.group(2) if date_num else ""

    # Тип акта ищем ТОЛЬКО в части до "от {дата}" (где он реально указан —
    # "Распоряжение Президента РФ от..."), а не по всему header_text. Раньше
    # искали по всему тексту, включая кавычки с заголовком — если заголовок
    # сам упоминает другой тип акта (например "О внесении изменений в
    # Федеральный закон..." у распоряжения), тип определялся неверно и
    # dedup_key переставал совпадать с тем же документом у RusLawOD.
    # Воспроизведено и исправлено 2026-10-07 на реальном акте №378-рп.
    type_search_text = header_text[:date_num.start()] if date_num else header_text

    doc_type = "НПА"
    for t in ["Федеральный конституционный закон", "Федеральный закон",
              "Указ", "Постановление", "Распоряжение", "Приказ", "Решение"]:
        if t.lower() in type_search_text.lower():
            doc_type = t
            break

    title_match = re.search(r'"([^"]{5,300})"', header_text)
    title = title_match.group(1) if title_match else (header_text[:200] or "Без названия")

    return {
        "doc_id": nd,
        "title": title,
        "date": date,
        "doc_type": doc_type,
        "doc_number": number,
        "dedup_key": make_dedup_key(doc_type, number, date),
        # Полный текст органа-издателя (тип акта + издатель, до "от {дата}") —
        # используется index_pravo_gov_remaining.py для определения региона/
        # ведомства ПО КАЖДОМУ ДОКУМЕНТУ отдельно (не по блоку крауглинга в
        # целом) — см. classify_authority() там же. Старые вызовы (4
        # федеральных блока, backfill_pravo_gov_url_ids.py) это поле просто
        # не используют, обратная совместимость не нарушена.
        "authority_text": type_search_text.strip(),
    }


def parse_doc_meta(html: str, nd: str) -> dict:
    """
    Извлекает реквизиты документа из блока <div class="document-name">...</div>
    страницы САМОГО ДОКУМЕНТА. Используется только как fallback — в норме
    реквизиты уже приходят из листинга, см. parse_listing_documents().
    """
    block = re.search(
        r'document-name[^"]*">(.*?)</div>\s*(?:</div>\s*)*<div',
        html, re.DOTALL,
    )
    return _parse_citation(block.group(1) if block else html, nd)


def parse_listing_documents(html: str) -> list[dict]:
    """
    Извлекает список документов СРАЗУ С РЕКВИЗИТАМИ прямо со страницы листинга
    (блок <a class="documents-item-name">) — без похода на страницу каждого
    документа по отдельности. Это и есть основная экономия запросов: раньше
    на дедуп-проверку уходил 1 запрос на КАЖДЫЙ документ, теперь — 0
    (только на страницы листинга, которые и так качаются).
    """
    pairs = re.findall(
        r'href="/document/(\d{16})"\s+class="documents-item-name">\s*(.*?)\s*</a>',
        html, re.DOTALL,
    )
    return [_parse_citation(text, nd) for nd, text in pairs]


# ── Главный цикл ────────────────────────────────────────────────────────────

async def fetch_page(session: aiohttp.ClientSession, url: str, retries: int = 3) -> str:
    for attempt in range(retries):
        try:
            async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=30)) as r:
                if r.status == 200:
                    return await r.text(encoding="utf-8", errors="replace")
                await asyncio.sleep(2 ** attempt)
        except Exception:
            await asyncio.sleep(2 ** attempt)
    return ""


async def fetch_bytes(session: aiohttp.ClientSession, url: str, retries: int = 3) -> bytes:
    for attempt in range(retries):
        try:
            async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=60)) as r:
                if r.status == 200:
                    return await r.read()
                await asyncio.sleep(2 ** attempt)
        except Exception:
            await asyncio.sleep(2 ** attempt)
    return b""


async def collect_documents(
    session: aiohttp.ClientSession,
    block: str,
    date_from: str,
    date_to: str,
) -> list[dict]:
    """
    Собирает все документы из блока за период — СРАЗУ с реквизитами и
    dedup_key, без единого запроса на страницу отдельного документа.
    """
    all_docs = []
    seen = set()
    index = 1
    total = None
    page_retries = 0
    pbar = tqdm(desc=f"  Страницы [{block}]", unit="стр", leave=False)

    while True:
        url = (
            f"{BASE_URL}/search?pageSize={PAGE_SIZE}&index={index}"
            f"&DocumentDateFrom={date_from}&DocumentDateTo={date_to}"
            f"&DocumentDateSearchType=0&SortedBy=6&SortDestination=1"
        )
        # Для блоков president/government используем block-специфичный поиск
        # (дата ОБЯЗАТЕЛЬНА и здесь — без неё сайт отдаёт документы за всю историю)
        if block in ("president", "government", "council_1", "council_2"):
            url = (
                f"{BASE_URL}/documents/block/{block}"
                f"?pageSize={PAGE_SIZE}&index={index}"
                f"&DocumentDateFrom={date_from}&DocumentDateTo={date_to}"
            )

        html = await fetch_page(session, url)
        if not html:
            # Пустой ответ при незавершённом сборе — вероятнее троттлинг,
            # чем реальный конец данных. Пробуем переждать, а не молча обрываем.
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

        # Останавливаемся по реальному общему числу записей с сайта, а не по
        # эвристике "есть ли ссылка 'next'" — она не совпадает с разметкой сайта.
        if total and len(all_docs) >= total:
            break
        if not new_docs:
            break  # страница не дала новых документов — защита от зацикливания
        index += 1
        await asyncio.sleep(0.3)  # rate limit

    pbar.close()
    return all_docs


# Защита от троттлинга: сайт не даёт DDoS Guard, но при серии запросов
# подряд начинает молчать/таймаутить (проверено эмпирически 2026-09-24).
# После нескольких подряд неудачных скачиваний PDF — длинная пауза,
# а не мгновенный переход к следующему документу.
CONSECUTIVE_FAILURES_THRESHOLD = 3
COOLDOWN_SECONDS = 90


async def process_documents(
    session: aiohttp.ClientSession,
    client: QdrantClient,
    model: SentenceTransformer | None,
    docs: list[dict],
    stats: dict,
    limit: int | None = None,
    dry_run: bool = False,
) -> bool:
    """
    Обрабатывает список документов (реквизиты и dedup_key уже есть из
    листинга — см. collect_documents). PDF скачивается ТОЛЬКО для тех,
    кто прошёл проверку на дубль.
    dry_run: не пишет в Qdrant (модель эмбеддинга не нужна, model может быть None).
    limit: общий предел обработанных документов (across всех блоков) — как
    только достигнут, возвращает False и вызывающий должен остановиться.
    """
    batch = []
    consecutive_failures = 0
    hit_limit = False

    for meta in tqdm(docs, desc="  Документы", leave=False):
        if limit is not None and stats["processed"] >= limit:
            hit_limit = True
            break
        stats["processed"] += 1
        nd = meta["doc_id"]

        if doc_exists(client, meta["dedup_key"]):
            stats["skipped_dup"] += 1
            continue

        if consecutive_failures >= CONSECUTIVE_FAILURES_THRESHOLD:
            tqdm.write(f"    {consecutive_failures} сбоев подряд — пауза {COOLDOWN_SECONDS}с")
            await asyncio.sleep(COOLDOWN_SECONDS)
            consecutive_failures = 0

        # Скачиваем PDF — только для документов, прошедших дедуп-проверку
        pdf_bytes = await fetch_bytes(
            session, f"{BASE_URL}/file/pdf?eoNumber={nd}"
        )
        if not pdf_bytes:
            stats["skipped_no_pdf"] += 1
            consecutive_failures += 1
            await asyncio.sleep(0.5)
            continue
        consecutive_failures = 0

        # Извлекаем текст (гибрид: PyMuPDF, при необходимости — OCR)
        text, used_ocr = extract_pdf_text(pdf_bytes)
        stats["ocr_used" if used_ocr else "text_layer"] += 1
        if len(text) < 100:
            stats["skipped_empty"] += 1
            continue

        # Чанкуем и добавляем в батч
        chunks = chunk_text(text)
        for idx, chunk in enumerate(chunks):
            chunk_item = {
                "doc_id":    nd,
                "title":     meta["title"],
                "text":      chunk,
                "doc_type":  meta["doc_type"],
                "date":      meta["date"],
                "chunk_idx": idx,
                "dedup_key": meta["dedup_key"] or "",
            }
            for key in _BLOCK_META_KEYS:
                if meta.get(key):
                    chunk_item[key] = meta[key]
            batch.append(chunk_item)
            stats["chunks"] += 1

            if not dry_run and len(batch) >= BATCH_SIZE:
                index_batch(client, model, batch)
                batch = []

        stats["indexed"] += 1
        await asyncio.sleep(0.5)  # rate limit

    if batch and not dry_run:
        index_batch(client, model, batch)
    return not hit_limit


async def main(date_from: str, date_to: str, limit: int | None, dry_run: bool):
    print("=== LegalSearch: индексация pravo.gov.ru ===\n")
    print(f"Период: {date_from} — {date_to}")
    print(f"Лимит:  {limit if limit else 'без ограничения'}")
    print(f"Режим:  {'dry-run (без индексации)' if dry_run else 'полная индексация'}\n")

    print("Подключаюсь к Qdrant...")
    client = QdrantClient(url=QDRANT_URL, timeout=60)
    ensure_payload_index(client)

    model = None
    if not dry_run:
        print(f"Загружаю модель {EMBEDDING_MODEL}...")
        model = SentenceTransformer(EMBEDDING_MODEL, device="cuda")

    stats = {"processed": 0, "indexed": 0, "skipped_dup": 0, "skipped_no_pdf": 0,
             "skipped_empty": 0, "chunks": 0, "ocr_used": 0, "text_layer": 0}

    async with aiohttp.ClientSession() as session:
        for block, block_name in FEDERAL_BLOCKS:
            if limit is not None and stats["processed"] >= limit:
                break
            print(f"\n[{block_name}]")
            docs = await collect_documents(session, block, date_from, date_to)
            print(f"  Найдено документов: {len(docs)}")

            if docs:
                keep_going = await process_documents(
                    session, client, model, docs, stats, limit=limit, dry_run=dry_run,
                )
                if not keep_going:
                    break

    print(f"\n✓ Готово!")
    print(f"  Обработано:                {stats['processed']:,}")
    print(f"  Проиндексировано:          {stats['indexed']:,}")
    print(f"  Пропущено (дубли):         {stats['skipped_dup']:,}")
    print(f"  Пропущено (нет PDF):       {stats['skipped_no_pdf']:,}")
    print(f"  Пропущено (пустой текст):  {stats['skipped_empty']:,}")
    print(f"  Загружено чанков:          {stats['chunks']:,}")
    print(f"  Текстовый слой (быстро):   {stats['text_layer']:,}")
    print(f"  Потребовался OCR:          {stats['ocr_used']:,}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    default_to = datetime.now().strftime("%d.%m.%Y")
    default_from = (datetime.now() - timedelta(days=730)).strftime("%d.%m.%Y")
    parser.add_argument("--date-from", default=default_from)
    parser.add_argument("--date-to", default=default_to)
    parser.add_argument("--limit", type=int, default=None,
                         help="Максимум обработанных документов (across всех блоков)")
    parser.add_argument("--dry-run", action="store_true",
                         help="Скачать и распознать, но не индексировать в Qdrant")
    args = parser.parse_args()

    asyncio.run(main(args.date_from, args.date_to, args.limit, args.dry_run))
