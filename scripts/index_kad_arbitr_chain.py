"""
Индексация судебных решений с kad.arbitr.ru С ЦЕПОЧКОЙ ИНСТАНЦИЙ
(первая инстанция -> апелляция -> кассация), в отличие от index_kad_arbitr.py,
который получает только ОДИН документ на дело через ras.arbitr.ru.

Стратегия (см. project_kad_arbitr_optimizations.md, раздел "Цепочка инстанций",
план подтверждён живыми проверками 2026-10-22, но не реализован):
  1. Ищем дела через поиск kad.arbitr.ru (как в index_kad_arbitr.py) —
     получаем case_number + case_id (GUID карточки) по категории/периоду.
  2. Для каждого найденного дела открываем /Card/{case_id} и парсим ВСЕ
     блоки <div class="b-chrono-item-header"> из HTML — это все инстанции
     дела (первая/апелляция/кассация...) за ОДИН заход, без доп. запросов.
  3. У каждого блока: instance_id (отдельный GUID на инстанцию — НЕ путать
     с case_id), instance_level (готовая метка "Первая инстанция" и т.п.),
     суд, дата, прямая ссылка на PDF. Блоки без PDF-ссылки — инстанция ещё
     не завершена (нет итогового акта), пропускаются.
  4. PDF скачивается НАПРЯМУЮ с kad.arbitr.ru (download_pdf с готовым
     pdf_url) — поход на ras.arbitr.ru здесь не нужен вообще, в отличие от
     index_kad_arbitr.py. Это убирает целый источник багов с деградацией
     сессии/блокировками, которые ловили на связке kad<->ras.
  5. Дедупликация — по doc_id = instance_id (не по case_number, который
     одинаков у первой инстанции и отличается у апелляции/кассации в своей
     нумерации — проверено живьём, см. память проекта).
  6. В Qdrant payload: case_id (общий ключ дела — позволяет потом одним
     фильтром вытащить всю цепочку), doc_id=instance_id, case_number,
     instance_level, court, court_code, date, pdf_url, title, text, source,
     category, chunk_idx.

index_kad_arbitr.py НЕ изменён и НЕ используется в проде этим файлом,
только как источник переиспользуемых функций (браузер, поиск дел по
категории, скачивание PDF, чанкинг, Qdrant-утилиты) — старый путь через
ras.arbitr.ru остаётся рабочим на случай отката.

Запуск:
    # Тест — 5 дел, не индексировать:
    python scripts/index_kad_arbitr_chain.py --probe

    # Тест на известном деле с полной цепочкой (3 инстанции, см. память):
    python scripts/index_kad_arbitr_chain.py --case-number А56-44984/2025 --dry-run

    # Боевой прогон:
    python scripts/index_kad_arbitr_chain.py --category tax --limit 2000
"""

import argparse
import hashlib
import io
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timedelta

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from sentence_transformers import SentenceTransformer
from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct, PayloadSchemaType

if hasattr(sys.stdout, 'buffer'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

from index_kad_arbitr import (
    KAD_URL, QDRANT_URL, COLLECTION_NAME, EMBEDDING_MODEL, BATCH_SIZE,
    DELAY_CASE, DELAY_PAGE, DELAY_RETRY, PDF_DOWNLOAD_DIR, CATEGORIES,
    init_driver, search_cases, extract_pdf_text, chunk_text,
    matches_category, doc_exists, _dismiss_popups, _is_rate_limited, _log_timeout_case,
)


class BanDetected(Exception):
    """
    Поднимается, когда признак блокировки (429/451/503) обнаружен на карточке
    дела (/Card/{case_id}) или на скачивании PDF, уже после ограниченного
    числа повторов. Раньше (до 2026-11-04) эти два места вообще не проверяли
    страницу на бан — забаненная карточка молча парсилась как "дело не
    завершено" (parse_case_status вернёт ""), а забаненное скачивание PDF —
    как обычный сбой скачивания, и скрипт продолжал открывать карточку за
    карточкой без остановки и без паузы. search_cases() (в index_kad_arbitr.py)
    такую проверку уже имел — её не было именно в НОВОМ коде этого файла
    (get_case_chain/download_pdf_via_navigation), который на каждое дело
    обращается к сайту заметно чаще, чем старый index_kad_arbitr.py. Это и
    есть наиболее вероятная причина бана 2026-11-04 после всего ~2 сессий.
    """


# Лог реальных обращений к сайту (не "запуск скрипта", а каждая отдельная
# навигация браузера) — чтобы в будущем честно сопоставлять нагрузку с банами,
# а не гадать по числу запусков скрипта (см. project_kad_arbitr_optimizations.md).
REQUEST_LOG_PATH = os.path.join(os.environ.get("TEMP", "C:/Windows/Temp"), "kad_arbitr_requests.log")


def _log_request(kind: str, detail: str = ""):
    try:
        with open(REQUEST_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')}\t{kind}\t{detail}\n")
    except Exception:
        pass


def download_pdf_via_navigation(driver: uc.Chrome, pdf_url: str, case_number: str = "") -> bytes:
    """
    Скачивает PDF с kad.arbitr.ru через ПРЯМУЮ НАВИГАЦИЮ браузера
    (driver.get), не через fetch()+blob() (как старый download_pdf() в
    index_kad_arbitr.py). Это не взаимозаменяемо: живой тест 2026-10-28
    показал, что fetch() на эндпоинт /Kad/PdfDocument/ получает страницу
    JS-челленджа DDoS-Guard (Server: ddos-guard, content-type: text/html)
    вместо файла — челлендж решается только при полноценной навигации
    (тот же механизм, что уже учтён в init_driver()/get_text_from_ras() для
    обхода DDoS-Guard на обычных страницах). Паттерн скопирован с уже
    проверенного скачивания в get_text_from_ras() ([index_kad_arbitr.py:353-406]).
    """
    os.makedirs(PDF_DOWNLOAD_DIR, exist_ok=True)
    before_files = set(os.listdir(PDF_DOWNLOAD_DIR))

    _rate_limited_final = False
    for _pdf_retry in range(2):
        _log_request("pdf", case_number)
        try:
            driver.get(pdf_url)
        except Exception as e:
            print(f"    [debug] driver.get(pdf_url) оборвался по таймауту: {e!r}")
            _log_timeout_case(case_number, pdf_url, "page_load_timeout")
            break

        try:
            _body = driver.find_element(By.TAG_NAME, "body").text[:300]
        except Exception:
            _body = ""
        if _is_rate_limited(_body):
            print(f"    [debug] 503/перегрузка на скачивании PDF: {_body[:150]!r}")
            _log_timeout_case(case_number, pdf_url, "503_service_unavailable")
            _rate_limited_final = True
            if _pdf_retry == 0:
                time.sleep(DELAY_RETRY)
                continue
        else:
            _rate_limited_final = False
        break

    if _rate_limited_final:
        raise BanDetected(
            f"Скачивание PDF ({case_number}) отдаёт признак блокировки (429/451/503) "
            f"после повтора — останавливаю прогон, не продолжаю стучаться."
        )

    pdf_path = None
    for _ in range(150):
        time.sleep(0.2)
        after_files = set(os.listdir(PDF_DOWNLOAD_DIR))
        new_files = after_files - before_files
        done = [f for f in new_files if not f.endswith(".crdownload")]
        if done:
            pdf_path = os.path.join(PDF_DOWNLOAD_DIR, done[0])
            break

    if not pdf_path:
        print("    [debug] файл не скачался за 30с")
        _log_timeout_case(case_number, pdf_url, "download_timeout_30s")
        return b""

    with open(pdf_path, "rb") as f:
        pdf_bytes = f.read()
    os.remove(pdf_path)

    if pdf_bytes[:4] != b'%PDF':
        print(f"    [debug] файл не PDF, header={pdf_bytes[:8]!r}")
        return b""

    return pdf_bytes

# ── Парсинг карточки дела ───────────────────────────────────────────────────

# Разбиваем HTML карточки по началу каждого блока инстанции — внутри одного
# куска между соседними разбиениями лежат все поля ОДНОЙ инстанции. Надёжнее,
# чем один большой регэксп на весь блок (вложенность div/span непостоянна).
_CHRONO_SPLIT_RE = re.compile(r'<div class="b-chrono-item-header')


def parse_chrono_blocks(html: str) -> list[dict]:
    """
    Извлекает все инстанции дела из HTML страницы /Card/{case_id}.
    Данные уже отрендерены сервером в HTML (не отдельный XHR/fetch — проверено
    живьём, перехватчик XHR ничего не ловит на этой странице, см. память).

    Возвращает список словарей, по одному на инстанцию, с полями:
    instance_id, court_code, instance_level, date, case_number, court,
    pdf_url, title. Если у инстанции ещё нет итогового документа (дело на
    этой стадии не завершено) — pdf_url будет пустой строкой, такие блоки
    отфильтровываются ВЫЗЫВАЮЩИМ кодом (здесь просто возвращаются как есть).
    """
    blocks = []
    parts = _CHRONO_SPLIT_RE.split(html)[1:]
    for part in parts:
        head = part[:500]  # атрибуты самого тега div — в начале куска

        id_m = re.search(r'data-id="([0-9a-f-]{36})"', head)
        if not id_m:
            continue
        instance_id = id_m.group(1)

        court_code_m = re.search(r'data-court="([^"]*)"', head)
        court_code = court_code_m.group(1) if court_code_m else ""

        level_m = re.search(
            r'<strong>\s*([^<]+?)\s*</strong>\s*<span class="b-reg-date">',
            part, re.DOTALL,
        )
        instance_level = level_m.group(1).strip() if level_m else ""

        date_m = re.search(r'<span class="b-reg-date">\s*([\d.]+)\s*</span>', part)
        date = date_m.group(1) if date_m else ""

        case_number_m = re.search(
            r'<strong class="b-case-instance-number">\s*([^<]+?)\s*</strong>', part,
        )
        case_number = case_number_m.group(1).strip() if case_number_m else ""

        court_m = re.search(
            r'class="instantion-name">\s*<a[^>]*>\s*([^<]+?)\s*</a>', part, re.DOTALL,
        )
        court = court_m.group(1).strip() if court_m else ""

        result_m = re.search(
            r'<h2 class="b-case-result">(.*?)</h2>', part, re.DOTALL,
        )
        pdf_url, title = "", ""
        if result_m:
            result_html = result_m.group(1)
            pdf_m = re.search(r'href="([^"]+\.pdf)"', result_html)
            pdf_url = pdf_m.group(1) if pdf_m else ""
            title_m = re.search(r'</i>\s*([^<]{3,300}?)\s*</a>', result_html, re.DOTALL)
            title = re.sub(r'\s+', ' ', title_m.group(1)).strip() if title_m else ""

        blocks.append({
            "instance_id": instance_id,
            "court_code": court_code,
            "instance_level": instance_level,
            "date": date,
            "case_number": case_number,
            "court": court,
            "pdf_url": pdf_url,
            "title": title,
        })
    return blocks


def parse_case_status(html: str) -> str:
    """
    Общий статус дела целиком — "Рассмотрение дела завершено" или
    "Рассматривается в N инстанции" и т.п. Лежит в <div class="b-case-header-desc">
    в шапке карточки, над списком инстанций (проверено на реальных сохранённых
    карточках, class одинаков и на завершённых, и на текущих делах).
    """
    m = re.search(r'<div class="b-case-header-desc">\s*([^<]+?)\s*</div>', html, re.DOTALL)
    return m.group(1).strip() if m else ""


CASE_CLOSED_STATUS = "рассмотрение дела завершено"


def get_case_chain(driver: uc.Chrome, case_id: str, _retry: int = 0) -> tuple[str, list[dict]]:
    """
    Открывает карточку дела и возвращает (статус дела целиком, список инстанций).
    См. parse_case_status/parse_chrono_blocks.

    Проверяет признак блокировки (429/451/503) на теле страницы — ЭТА карточка
    открывается на КАЖДОЕ дело (основной источник нагрузки на сайт в этом
    скрипте), и до 2026-11-04 эта проверка здесь отсутствовала: забаненная
    страница молча парсилась как "дело не завершено" и скрипт продолжал
    открывать карточку за карточкой без остановки, см. BanDetected.
    """
    _log_request("card", case_id)
    driver.get(f"{KAD_URL}/Card/{case_id}")
    time.sleep(2)
    _dismiss_popups(driver)
    try:
        _body = driver.find_element(By.TAG_NAME, "body").text[:300]
    except Exception:
        _body = ""
    if _is_rate_limited(_body):
        if _retry < 1:
            print(f"    ⚠ Признак блокировки (429/451/503) на карточке дела — "
                  f"пауза {DELAY_RETRY:.0f}с перед повтором...")
            time.sleep(DELAY_RETRY)
            return get_case_chain(driver, case_id, _retry=_retry + 1)
        raise BanDetected(
            f"Карточка дела {case_id} отдаёт признак блокировки (429/451/503) "
            f"после повтора: {_body[:150]!r}"
        )
    html = driver.page_source
    return parse_case_status(html), parse_chrono_blocks(html)


def find_case_id_by_number(driver: uc.Chrome, case_number: str) -> str:
    """
    Находит case_id (GUID карточки) по номеру дела через главный поиск на
    kad.arbitr.ru — тот же способ, что использовался при живой проверке
    плана (см. память проекта). Нужен для --case-number (точечный тест на
    известном деле), в обычном боевом прогоне case_id уже приходит из
    search_cases().
    """
    driver.get(f"{KAD_URL}/")
    time.sleep(2)
    _dismiss_popups(driver)

    inp = None
    for el in driver.find_elements(By.XPATH, "//input[@type='text' or not(@type)]"):
        ph = el.get_attribute("placeholder") or ""
        if "А50" in ph:
            inp = el
            break
    if inp is None:
        return ""

    driver.execute_script("""
        var el = arguments[0], val = arguments[1];
        var nativeSetter = Object.getOwnPropertyDescriptor(
            window.HTMLInputElement.prototype, 'value').set;
        nativeSetter.call(el, val);
        el.dispatchEvent(new Event('input', {bubbles: true}));
        el.dispatchEvent(new Event('change', {bubbles: true}));
        el.dispatchEvent(new FocusEvent('blur', {bubbles: true}));
        el.blur();
    """, inp, case_number)
    time.sleep(0.5)

    try:
        btn = driver.find_element(By.XPATH, "//button[contains(text(),'Найти')]")
        driver.execute_script("arguments[0].click();", btn)
    except Exception:
        return ""

    try:
        WebDriverWait(driver, 15).until(
            lambda d: d.find_elements(By.XPATH, "//a[contains(@href,'/Card/')]")
        )
    except Exception:
        return ""

    links = driver.find_elements(By.XPATH, "//a[contains(@href,'/Card/')]")
    if not links:
        return ""
    m = re.search(r"/Card/([0-9a-f-]{36})", links[0].get_attribute("href") or "")
    return m.group(1) if m else ""


# ── Qdrant ───────────────────────────────────────────────────────────────────

def ensure_payload_index_chain(client: QdrantClient):
    """
    Индекс на doc_id (= instance_id, дедупликация) переиспользуется из
    index_kad_arbitr.py (та же коллекция, то же поле). Дополнительно — индекс
    на case_id, чтобы фильтр "вся цепочка этого дела" на стороне поиска
    (backend) работал быстро, а не полным перебором.
    """
    for field in ("doc_id", "case_id"):
        try:
            client.create_payload_index(
                collection_name=COLLECTION_NAME,
                field_name=field,
                field_schema=PayloadSchemaType.KEYWORD,
            )
        except Exception:
            pass


def index_batch_chain(client: QdrantClient, model: SentenceTransformer, batch: list[dict]):
    texts = [f"passage: {item['text']}" for item in batch]
    embeddings = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
    points = [
        PointStruct(
            id=str(uuid.uuid4()),
            vector=embeddings[i].tolist(),
            payload={
                "doc_id":         item["instance_id"],
                "case_id":        item["case_id"],
                "case_number":    item["case_number"],
                "instance_level": item["instance_level"],
                "court":          item["court"],
                "court_code":     item["court_code"],
                "title":          item["title"],
                "text":           item["text"],
                "doc_type":       "Судебное решение",
                "date":           item["date"],
                "pdf_url":        item["pdf_url"],
                "case_url":       item["case_url"],
                "chunk_idx":      item["chunk_idx"],
                "source":         "kad.arbitr.ru",
                "category":       item["category"],
            },
        )
        for i, item in enumerate(batch)
    ]
    client.upsert(collection_name=COLLECTION_NAME, points=points)


# ── Чекпоинт (резюме после бана) ────────────────────────────────────────────
# Если прогон остановился из-за бана (BanDetected) или поиск отдал пустой
# ответ после исчерпанных повторов (search_cases в index_kad_arbitr.py уже
# сам ретраит и возвращает {} только когда блокировка не снимается — это
# тоже признак бана, не "данные закончились"), прогресс сохраняется в файл.
# Следующий запуск с ТЕМИ ЖЕ --category/--date-from/--date-to/--limit
# автоматически подхватывает сохранённую страницу поиска, набранное
# cases_new и уже просмотренные case_id — не нужно начинать с нуля после
# снятия бана. Добавлено по явному запросу пользователя 2026-11-04.

def _checkpoint_path(category_key: str) -> str:
    return os.path.join(os.environ.get("TEMP", "C:/Windows/Temp"), f"kad_arbitr_checkpoint_{category_key}.json")


def _load_checkpoint(category_key: str, date_from: str, date_to: str, limit: int) -> dict | None:
    path = _checkpoint_path(category_key)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    if (data.get("date_from") != date_from or data.get("date_to") != date_to
            or data.get("limit") != limit):
        print(f"  (найден чекпоинт {path}, но параметры не совпадают — игнорирую)")
        return None
    return data


def _save_checkpoint(category_key: str, date_from: str, date_to: str, limit: int,
                      page: int, cases_new: int, seen_case_ids: set[str]):
    path = _checkpoint_path(category_key)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({
                "date_from": date_from, "date_to": date_to, "limit": limit,
                "page": page, "cases_new": cases_new,
                "seen_case_ids": list(seen_case_ids),
                "saved_at": datetime.now().isoformat(timespec="seconds"),
            }, f, ensure_ascii=False)
    except Exception as e:
        print(f"  ⚠ Не удалось сохранить чекпоинт: {e!r}")


def _delete_checkpoint(category_key: str):
    try:
        os.remove(_checkpoint_path(category_key))
    except FileNotFoundError:
        pass
    except Exception:
        pass


# ── Главный цикл ────────────────────────────────────────────────────────────

def run(
    category_key: str,
    limit: int,
    date_from: str,
    date_to: str,
    probe: bool,
    dry_run: bool,
    headless: bool = False,
    shard: tuple[int, int] | None = None,
    window_pos: tuple[int, int] | None = None,
    single_case_number: str | None = None,
    fresh: bool = False,
):
    cat = CATEGORIES[category_key]
    _run_start = time.time()
    print(f"\nКатегория:  {cat['label']}")
    if single_case_number:
        print(f"Точечный тест по номеру дела: {single_case_number}")
    else:
        print(f"Период:     {date_from[:10]} — {date_to[:10]}")
        print(f"Лимит:      {limit} дел")
    print(f"Режим:      {'probe' if probe else 'dry-run (без индексации)' if dry_run else 'полная индексация'}")
    if shard is not None:
        print(f"Шард:       {shard[0]}/{shard[1]}")
    print()

    driver = init_driver(headless=headless, window_pos=window_pos)

    try:
        if not dry_run and not probe:
            print("Подключаюсь к Qdrant...")
            client = QdrantClient(url=QDRANT_URL, timeout=60)
            ensure_payload_index_chain(client)
            print("Загружаю модель эмбеддинга...")
            import torch
            device = "cuda" if torch.cuda.is_available() else "cpu"
            print(f"  Устройство: {device}")
            model = SentenceTransformer(EMBEDDING_MODEL, device=device)
        else:
            client = model = None

        stats = {
            "cases_processed": 0, "cases_new": 0, "cases_skipped_seen": 0, "skipped_not_closed": 0,
            "instances_found": 0, "instances_indexed": 0,
            "skipped_dup": 0, "skipped_no_pdf_yet": 0, "skipped_download_failed": 0,
            "skipped_category": 0, "chunks": 0,
        }
        batch = []
        # case_id общий на дело, а номер дела — свой у каждой инстанции
        # (13АП-xxx у апелляции ≠ А56-xxx у первой инстанции) — если поиск
        # вернёт оба номера отдельными строками, они укажут на один и тот же
        # case_id. Дедуп по instance_id ниже всё равно не даст задвоить
        # данные, но без этого набора карточка открывалась бы заново —
        # лишний запрос к сайту без необходимости. НЕ исключает case_id
        # между разными ЗАПУСКАМИ скрипта (там это не лишняя работа, а
        # единственный способ подхватить новую инстанцию, появившуюся с
        # прошлого раза) — только в рамках этого прогона.
        seen_case_ids: set[str] = set()

        # ── Точечный тест на одном известном деле ───────────────────────────
        if single_case_number:
            case_id = find_case_id_by_number(driver, single_case_number)
            if not case_id:
                print(f"ОШИБКА: не нашёл case_id для {single_case_number!r}")
                return
            print(f"case_id = {case_id}\n")
            cases_to_process = [(single_case_number, case_id)]
        else:
            cases_to_process = None  # обрабатывается постранично в цикле ниже

        # ── Probe: только показать первые дела и их цепочки ─────────────────
        if probe:
            data = search_cases(driver, cat["case_type"], date_from, date_to, page=1)
            if not data:
                print("ОШИБКА: поиск не ответил.")
                return
            items = data.get("Result", {}).get("Items", [])[:5]
            for item in items:
                case_number = item.get("CaseNumber", "—")
                case_id = item.get("CaseId", "")
                print(f"{case_number}  (case_id={case_id})")
                status, chain = get_case_chain(driver, case_id)
                print(f"    статус дела: {status!r}")
                for b in chain:
                    has_pdf = "есть PDF" if b["pdf_url"] else "нет PDF (не завершена)"
                    print(f"    [{b['instance_level'] or '?'}] {b['case_number']} | "
                          f"{b['court'][:40]} | {b['date']} | {has_pdf}")
                print()
                time.sleep(DELAY_CASE)
            return

        def process_case(case_number: str, case_id: str) -> bool:
            """
            Возвращает True, если дело добавило в базу хотя бы одну НОВУЮ
            инстанцию (не дубль, завершена, подошла по категории) — именно
            это и считает --limit теперь (цель "N реально проиндексированных
            дел", а не "N просмотренных", см. память проекта). Дело, которое
            отсеялось по ЛЮБОЙ причине (дубль / не завершено / не та
            категория / сбой скачивания), в счёт лимита не идёт — вызывающий
            код продолжит искать дальше, пока не наберётся нужное число.
            """
            if case_id in seen_case_ids:
                stats["cases_skipped_seen"] += 1
                print(f"  [{case_number}] case_id уже обработан в этом прогоне, пропускаю")
                return False
            seen_case_ids.add(case_id)

            stats["cases_processed"] += 1
            print(f"  [{stats['cases_processed']:04d}] {case_number}  (case_id={case_id})")

            status, chain = get_case_chain(driver, case_id)
            if status.strip().lower() != CASE_CLOSED_STATUS:
                stats["skipped_not_closed"] += 1
                print(f"         дело не завершено целиком ({status!r}), пропускаю всю цепочку")
                return False

            stats["instances_found"] += len(chain)
            case_contributed = False

            for b in chain:
                if not b["pdf_url"]:
                    stats["skipped_no_pdf_yet"] += 1
                    continue

                instance_id = b["instance_id"]
                if not dry_run and doc_exists(client, instance_id):
                    stats["skipped_dup"] += 1
                    print(f"         [{b['instance_level']}] дубль, пропускаю")
                    continue

                pdf_bytes = download_pdf_via_navigation(driver, b["pdf_url"], case_number=b["case_number"])
                if not pdf_bytes:
                    stats["skipped_download_failed"] += 1
                    print(f"         [{b['instance_level']}] PDF не скачался")
                    continue

                text = extract_pdf_text(pdf_bytes)
                if len(text) < 200:
                    stats["skipped_download_failed"] += 1
                    print(f"         [{b['instance_level']}] текст слишком короткий")
                    continue

                if not matches_category(text, cat["keywords"]):
                    stats["skipped_category"] += 1
                    print(f"         [{b['instance_level']}] не соответствует категории")
                    continue

                print(f"         [{b['instance_level']}] OK, текст {len(text)} симв.")
                stats["instances_indexed"] += 1
                case_contributed = True

                if dry_run:
                    continue

                chunks = chunk_text(text)
                for idx, chunk in enumerate(chunks):
                    batch.append({
                        "instance_id":    instance_id,
                        "case_id":        case_id,
                        "case_number":    b["case_number"],
                        "instance_level": b["instance_level"],
                        "court":          b["court"],
                        "court_code":     b["court_code"],
                        "title":          b["title"] or f"{b['case_number']} — {b['court']}",
                        "text":           chunk,
                        "date":           b["date"],
                        "pdf_url":        b["pdf_url"],
                        "case_url":       f"{KAD_URL}/Card/{case_id}",
                        "chunk_idx":      idx,
                        "category":       category_key,
                    })
                    stats["chunks"] += 1

                if not dry_run and len(batch) >= BATCH_SIZE:
                    index_batch_chain(client, model, batch)
                    batch.clear()

            time.sleep(DELAY_CASE)
            if case_contributed:
                stats["cases_new"] += 1
            return case_contributed

        if cases_to_process:
            for case_number, case_id in cases_to_process:
                process_case(case_number, case_id)
        else:
            page = 1
            if fresh:
                _delete_checkpoint(category_key)
            else:
                ckpt = _load_checkpoint(category_key, date_from, date_to, limit)
                if ckpt:
                    page = ckpt["page"]
                    stats["cases_new"] = ckpt["cases_new"]
                    seen_case_ids.update(ckpt["seen_case_ids"])
                    print(f"  Обнаружен чекпоинт (сохранён {ckpt['saved_at']}): "
                          f"страница {page}, набрано {stats['cases_new']} из {limit} — продолжаю оттуда.")

            # --limit теперь — ЦЕЛЬ по реально проиндексированным делам
            # (stats["cases_new"]), а не по просмотренным (stats["cases_processed"]).
            # Раньше (index_kad_arbitr.py и первая версия этого файла) лимит считал
            # просмотренные — на практике реально проиндексированных оказывалось
            # заметно меньше (категория/дубли/незавершённость отсеивают большую
            # часть). Изменено по явному запросу пользователя 2026-11-04: нужно
            # гарантированное число РЕАЛЬНО новых дел, а не попыток.
            banned = False
            while stats["cases_new"] < limit:
                _log_request("search", f"page={page}")
                data = search_cases(driver, cat["case_type"], date_from, date_to, page=page)
                if not data:
                    # search_cases() сам ретраит на 429/451/503 и возвращает {}
                    # только когда блокировка НЕ снялась — это тоже бан, не
                    # "данных больше нет", поэтому сохраняем чекпоинт, а не
                    # считаем цель исчерпанной.
                    print("Поиск вернул пустой ответ после исчерпанных повторов — похоже на бан, останавливаюсь.")
                    banned = True
                    break
                items = data.get("Result", {}).get("Items", [])
                if not items:
                    print(f"Больше дел нет (набрано {stats['cases_new']} из {limit} целевых).")
                    break

                try:
                    for item in items:
                        if stats["cases_new"] >= limit:
                            break
                        case_number = item.get("CaseNumber", "—")
                        case_id = item.get("CaseId", "")

                        if shard is not None:
                            shard_index, shard_total = shard
                            case_hash = int(hashlib.md5(case_number.encode("utf-8")).hexdigest(), 16)
                            if case_hash % shard_total != shard_index:
                                continue

                        process_case(case_number, case_id)
                except BanDetected as e:
                    print(f"\n  ⚠ БАН обнаружен: {e}")
                    print(f"  Останавливаю прогон немедленно (не продолжаю стучаться по следующим делам).")
                    print(f"  Подождите (обычно 1-24ч) и запустите ТУ ЖЕ команду — продолжит с сохранённого места.")
                    banned = True
                    break

                page += 1
                _save_checkpoint(category_key, date_from, date_to, limit, page, stats["cases_new"], seen_case_ids)
                time.sleep(DELAY_PAGE)

            if banned:
                _save_checkpoint(category_key, date_from, date_to, limit, page, stats["cases_new"], seen_case_ids)
            else:
                # Цель достигнута или результаты сайта честно исчерпаны —
                # резюмировать больше нечего, чекпоинт не нужен.
                _delete_checkpoint(category_key)

        if batch and not dry_run:
            index_batch_chain(client, model, batch)

        elapsed = time.time() - _run_start
        print(f"\n{'='*60}")
        print(f"ИТОГИ [{cat['label']}]")
        print(f"  Дел реально проиндексировано (цель --limit): {stats['cases_new']:,} / {limit:,}")
        print(f"  Дел просмотрено всего:       {stats['cases_processed']:,}")
        print(f"  Дел пропущено (тот же case_id в этом прогоне): {stats['cases_skipped_seen']:,}")
        print(f"  Дел пропущено (не завершено целиком): {stats['skipped_not_closed']:,}")
        print(f"  Инстанций найдено в карточках: {stats['instances_found']:,}")
        print(f"  Проиндексировано инстанций:  {stats['instances_indexed']:,}")
        print(f"  Пропущено (дубли):           {stats['skipped_dup']:,}")
        print(f"  Пропущено (нет акта пока):   {stats['skipped_no_pdf_yet']:,}")
        print(f"  Пропущено (сбой скачивания): {stats['skipped_download_failed']:,}")
        print(f"  Пропущено (не та категория): {stats['skipped_category']:,}")
        if not dry_run:
            print(f"  Загружено чанков:            {stats['chunks']:,}")
        print(f"\n  Elapsed: {elapsed:.0f}с")

    finally:
        driver.quit()


def main():
    parser = argparse.ArgumentParser(description="Индексация kad.arbitr.ru с цепочкой инстанций")
    parser.add_argument("--category", choices=list(CATEGORIES.keys()), default="tax")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--probe", action="store_true",
                         help="Показать первые 5 дел и их цепочки инстанций, без индексации")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--date-from", default=None)
    parser.add_argument("--date-to", default=None)
    parser.add_argument("--headless", action="store_true",
                         help="НЕ использовать на kad.arbitr.ru — ломает валидацию формы")
    parser.add_argument("--shard", default=None, help="N/M, как в index_kad_arbitr.py")
    parser.add_argument("--window-pos", default=None, help="X,Y — как в index_kad_arbitr.py")
    parser.add_argument("--case-number", default=None,
                         help="Точечный тест на одном известном деле по номеру "
                              "(например А56-44984/2025) — находит case_id и "
                              "обрабатывает только эту цепочку, игнорирует --limit/--category-период")
    parser.add_argument("--fresh", action="store_true",
                         help="Игнорировать и удалить сохранённый чекпоинт, начать с чистого листа "
                              "(по умолчанию скрипт сам резюмирует с сохранённого места при совпадении "
                              "--category/--date-from/--date-to/--limit)")
    args = parser.parse_args()

    today = datetime.now()
    date_to = args.date_to or today.strftime("%Y-%m-%dT23:59:59")
    date_from = args.date_from or (today - timedelta(days=3 * 365)).strftime("%Y-%m-%dT00:00:00")

    shard = None
    if args.shard:
        idx_str, total_str = args.shard.split("/")
        shard = (int(idx_str), int(total_str))

    window_pos = None
    if args.window_pos:
        x_str, y_str = args.window_pos.split(",")
        window_pos = (int(x_str), int(y_str))

    run(
        category_key=args.category,
        limit=args.limit,
        date_from=date_from,
        date_to=date_to,
        probe=args.probe,
        dry_run=args.dry_run,
        headless=args.headless,
        shard=shard,
        window_pos=window_pos,
        single_case_number=args.case_number,
        fresh=args.fresh,
    )


if __name__ == "__main__":
    main()
