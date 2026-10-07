"""
Парсинг и индексация судебных решений с kad.arbitr.ru.

Стратегия:
  1. Ищем дела через POST /Kad/SearchInstances (апелляция + кассация, последние 3 года)
  2. Фильтруем по категории (налог / договор / корпоратив / банкротство)
  3. Для каждого дела скачиваем PDF через GET /Kad/PdfDocument/{doc_id}
  4. Извлекаем текст (PyMuPDF — PDF с текстовым слоем, OCR не нужен)
  5. Дедуплицируем по номеру дела и индексируем в Qdrant

ВАЖНО: kad.arbitr.ru блокирует IP при > ~10 запросов/мин (ответ 429/451).
        Скрипт детектирует 429/451 в тексте страницы (и на kad.arbitr.ru, и на
        ras.arbitr.ru) и делает паузу DELAY_RETRY (60с), с ограниченным числом
        повторов в search_cases(). Это помогает при кратковременном троттлинге,
        но НЕ от полноценного многочасового бана — если после нескольких попыток
        блокировка не снимается, скрипт останавливается с соответствующим
        сообщением, и нужно подождать 1–24 часа перед следующим запуском.

Запуск:
    # Тест — 5 дел, не индексировать, только показать что найдено:
    python scripts/index_kad_arbitr.py --probe

    # Тест — скачать и проиндексировать 10 дел (налоговые):
    python scripts/index_kad_arbitr.py --category tax --limit 10

    # MVP — 2000 дел на категорию:
    python scripts/index_kad_arbitr.py --category tax --limit 2000
    python scripts/index_kad_arbitr.py --category contract --limit 2000
    python scripts/index_kad_arbitr.py --category corporate --limit 2000
    python scripts/index_kad_arbitr.py --category bankruptcy --limit 2000

    # Параллельно на один период (сайт не фильтрует категории сам, оба
    # терминала иначе читают один и тот же список дел) — делим по --shard:
    python scripts/index_kad_arbitr.py --category tax --limit 1000 --shard 0/2
    python scripts/index_kad_arbitr.py --category contract --limit 1000 --shard 1/2
"""

import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta

import fitz  # PyMuPDF
import requests
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from sentence_transformers import SentenceTransformer
from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct, Filter, FieldCondition, MatchValue

if hasattr(sys.stdout, 'buffer'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# ── Константы ──────────────────────────────────────────────────────────────

QDRANT_URL      = "http://localhost:6333"
COLLECTION_NAME = "public_docs"
EMBEDDING_MODEL = "intfloat/multilingual-e5-large"

CHUNK_SIZE    = 400
CHUNK_OVERLAP = 50
BATCH_SIZE    = 32

KAD_URL     = "https://kad.arbitr.ru"
SEARCH_URL  = f"{KAD_URL}/Kad/SearchInstances"
DOCS_URL    = f"{KAD_URL}/Kad/InstanceDocuments"
PAGE_SIZE   = 25   # дел за один запрос к поиску

PDF_DOWNLOAD_DIR = os.path.join(os.environ.get("TEMP", "C:/Windows/Temp"), f"kad_pdf_dl_{os.getpid()}")

# Общий файл (на всех терминалов сразу) со списком дел, где скачивание PDF
# зависло/прервалось по таймауту — чтобы потом вручную проверить руками,
# не был ли файл просто необычно большим.
TIMEOUT_LOG_PATH = os.path.join(os.environ.get("TEMP", "C:/Windows/Temp"), "kad_timeout_cases.log")


def _log_timeout_case(case_number: str, pdf_url: str, reason: str):
    try:
        with open(TIMEOUT_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')}\t{case_number}\t{reason}\t{pdf_url}\n")
    except Exception:
        pass

# Задержки (секунды) — не уменьшать, иначе IP-бан (451)
DELAY_CASE   = 1.0   # между делами (UI сам добавляет ~4-5с на взаимодействие)
DELAY_PAGE   = 2.0   # между страницами поиска
DELAY_RETRY  = 60.0  # при получении 429/451 (повтор через минуту)

HEADERS = {
    "User-Agent":       "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Content-Type":     "application/json",
    "X-Requested-With": "XMLHttpRequest",
    "x-date-format":    "iso",
    "Referer":          "https://kad.arbitr.ru/",
    "Accept":           "application/json, text/javascript, */*; q=0.01",
}

# Категории: ключевые слова для фильтрации текста решения
CATEGORIES = {
    "tax": {
        "label": "Налоговые споры",
        "keywords": ["налог", "ндс", "усн", "ифнс", "фнс", "налоговая инспекция",
                     "налоговый вычет", "недоимка", "налогооблагаемая база"],
        "case_type": "А",
    },
    "contract": {
        "label": "Договорные споры",
        "keywords": ["договор", "неосновательное обогащение", "неоплата",
                     "задолженность", "поставка", "подряд", "аренда",
                     "купли-продажи", "неустойка"],
        "case_type": "А",
    },
    "corporate": {
        "label": "Корпоративные споры",
        "keywords": ["субсидиарная ответственность", "вывод активов", "участник ООО",
                     "выход из общества", "корпоративный спор", "директор",
                     "оспаривание сделки", "убытки общества"],
        "case_type": "А",
    },
    "bankruptcy": {
        "label": "Банкротство",
        "keywords": ["банкротств", "несостоятельность", "конкурсный управляющий",
                     "кредитор", "реестр требований", "финансовый управляющий"],
        "case_type": "Б",
    },
}

# ── Текстовые утилиты ───────────────────────────────────────────────────────

def chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    words = text.split()
    if not words:
        return []
    chunks, start = [], 0
    while start < len(words):
        end = start + size
        chunks.append(" ".join(words[start:end]))
        if end >= len(words):
            break
        start += size - overlap
    return chunks


def clean_text(text: str) -> str:
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def extract_pdf_text(pdf_bytes: bytes) -> str:
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        pages = [page.get_text() for page in doc]
        return clean_text("\n".join(pages))
    except Exception:
        return ""


RAS_URL = "https://ras.arbitr.ru/"


def _ras_dismiss_modals(driver: uc.Chrome):
    """Закрывает модальные окна на ras.arbitr.ru (Escape + клик X)."""
    try:
        from selenium.webdriver.common.keys import Keys as _Keys
        driver.find_element(By.TAG_NAME, "body").send_keys(_Keys.ESCAPE)
        time.sleep(0.3)
        # Кликаем кнопку закрытия если она видима
        for sel in [
            "button.close", ".modal .close", ".modal-close",
            "button[aria-label='Close']", "button[aria-label='Закрыть']",
        ]:
            els = driver.find_elements(By.CSS_SELECTOR, sel)
            for el in els:
                try:
                    if el.is_displayed():
                        driver.execute_script("arguments[0].click();", el)
                        time.sleep(0.2)
                except Exception:
                    pass
    except Exception:
        pass


def _ras_fill_input(driver: uc.Chrome, inp_el, value: str):
    """
    Заполняет React/Vue поле через нативный setter + input/change/blur события.
    blur обязателен: без него сайт не считывает введённое значение в реальное
    состояние формы (DOM value при этом выглядит корректно) и при клике "Найти"
    отдаёт нефильтрованную выдачу всего банка решений вместо результата по
    номеру дела — воспроизведено и подтверждено диагностикой 2026-10-01.
    """
    driver.execute_script("""
        var el = arguments[0], val = arguments[1];
        var nativeSetter = Object.getOwnPropertyDescriptor(
            window.HTMLInputElement.prototype, 'value').set;
        nativeSetter.call(el, val);
        el.dispatchEvent(new Event('input', {bubbles: true}));
        el.dispatchEvent(new Event('change', {bubbles: true}));
        el.dispatchEvent(new FocusEvent('blur', {bubbles: true}));
        el.blur();
    """, inp_el, value)


def get_text_from_ras(driver: uc.Chrome, case_number: str, case_id: str = "") -> str:
    """
    Ищет решение по номеру дела на ras.arbitr.ru и возвращает текст.
    case_id — GUID из kad.arbitr.ru (используется для точного выбора PDF среди результатов).
    Пример: case_number = 'А82-11153/2024', case_id = '7eb2610d-...'
    """
    try:
        # Переходим на ras.arbitr.ru если нужно
        if driver.current_url.rstrip("/") != "https://ras.arbitr.ru":
            driver.get(RAS_URL)

        # Ждём пока React инициализируется: input с placeholder "например, А50-5568/08"
        def _case_input_ready(d):
            inps = d.find_elements(By.XPATH, "//input[@type='text' or not(@type)]")
            return any(
                "А" in (i.get_attribute("placeholder") or "")
                and "-" in (i.get_attribute("placeholder") or "")
                and "/" in (i.get_attribute("placeholder") or "")
                for i in inps
            )
        try:
            WebDriverWait(driver, 10).until(_case_input_ready)
        except Exception:
            time.sleep(3)

        # Закрываем модальные окна (на каждое дело — see project_kad_arbitr_optimizations.md:
        # вызов "один раз за сессию" ломал поиск со 2-го дела, реальная причина не PDF-via-requests)
        _ras_dismiss_modals(driver)

        # Находим поле "Номер дела"
        all_inputs = driver.find_elements(By.XPATH, "//input[@type='text' or not(@type)]")
        case_inp = None
        for inp in all_inputs:
            ph = inp.get_attribute("placeholder") or ""
            if "А" in ph and "-" in ph and "/" in ph:
                case_inp = inp
                break
            if "А50" in ph or "номер" in ph.lower():
                case_inp = inp
                break

        if case_inp is None:
            return ""

        # Фокусируем и заполняем через нативный setter (React-совместимо)
        driver.execute_script("arguments[0].focus(); arguments[0].click();", case_inp)
        time.sleep(0.2)
        _ras_fill_input(driver, case_inp, case_number)
        time.sleep(0.5)

        # Кликаем кнопку "Найти" (надёжнее чем Enter в React-форме)
        try:
            search_btn = driver.find_element(By.XPATH,
                "//button[normalize-space(text())='Найти']")
            driver.execute_script("arguments[0].click();", search_btn)
        except Exception:
            from selenium.webdriver.common.keys import Keys as _Keys
            case_inp.send_keys(_Keys.ENTER)

        # Ждём появления PDF-ссылок (до 8с)
        pdf_links_now = []
        for _ in range(40):
            time.sleep(0.2)
            pdf_links_now = driver.find_elements(By.XPATH,
                "//a[contains(@href,'PdfDocument') or "
                "    (contains(@href,'ras.arbitr.ru') and contains(@href,'.pdf'))]")
            if pdf_links_now:
                break

        pdf_links = pdf_links_now
        print(f"    [ras debug] PDF links: {len(pdf_links)}")

        if not pdf_links:
            body_text = driver.find_element(By.TAG_NAME, "body").text[:300]
            print(f"    [ras debug] body (нет PDF): {body_text!r}")
            if _is_rate_limited(body_text):
                print(f"    ⚠ Обнаружен признак блокировки (429/451) на ras.arbitr.ru — "
                      f"пауза {DELAY_RETRY:.0f}с")
                time.sleep(DELAY_RETRY)
            return ""

        all_pdf_hrefs = [a.get_attribute("href") or "" for a in pdf_links]

        # Если есть case_id, выбираем PDF именно этого дела
        pdf_url = ""
        if case_id:
            matching = [h for h in all_pdf_hrefs if case_id.lower() in h.lower()]
            if matching:
                pdf_url = matching[0]
        if not pdf_url:
            pdf_url = all_pdf_hrefs[0]

        print(f"    [ras debug] PDF: {pdf_url}")
        if not pdf_url:
            return ""

        # Скачиваем PDF через навигацию браузера (Sec-Fetch-Dest: document)
        # Chrome скачивает файл благодаря plugins.always_open_pdf_externally=True
        before_files = set(os.listdir(PDF_DOWNLOAD_DIR))
        for _pdf_retry in range(2):
            try:
                driver.get(pdf_url)
            except Exception as e:
                # С driver.set_page_load_timeout(30) это сработает максимум
                # через 30с вместо зависания на дефолте Chrome (воспроизведено
                # как зависание >5 минут, см. память проекта, 2026-10-01).
                print(f"    [ras debug] driver.get(pdf_url) оборвался по таймауту: {e!r}")
                _log_timeout_case(case_number, pdf_url, "page_load_timeout")
                break

            # 503 — настоящая серверная перегрузка, подтверждено пользователем
            # вживую 2026-10-01 (конкретное дело из этого же лога). Раньше
            # молча считалось "файл не скачался за 30с" — пауза и один повтор.
            try:
                _body = driver.find_element(By.TAG_NAME, "body").text[:300]
            except Exception:
                _body = ""
            if _is_rate_limited(_body):
                print(f"    [ras debug] 503/перегрузка на скачивании PDF: {_body[:150]!r}")
                _log_timeout_case(case_number, pdf_url, "503_service_unavailable")
                if _pdf_retry == 0:
                    time.sleep(DELAY_RETRY)
                    continue
            break

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
            print("    [ras debug] файл не скачался за 30с")
            _log_timeout_case(case_number, pdf_url, "download_timeout_30s")
            return ""

        print(f"    [ras debug] скачан: {os.path.basename(pdf_path)}, "
              f"size={os.path.getsize(pdf_path)}")

        with open(pdf_path, "rb") as f:
            pdf_bytes = f.read()
        os.remove(pdf_path)

        if pdf_bytes[:4] != b'%PDF':
            print(f"    [ras debug] файл не PDF, header={pdf_bytes[:8]!r}")
            return ""

        text = extract_pdf_text(pdf_bytes)
        print(f"    [ras debug] текст: {len(text)} символов")
        return text

    except Exception as e:
        print(f"    [ras debug] исключение: {e}")
        return ""


def extract_page_text(driver: uc.Chrome, url: str) -> str:
    """Извлекает текст с HTML-страницы решения (PublishReport и аналогичные)."""
    try:
        driver.get(url)
        # Ждём появления содержательного текста (не просто скелет страницы)
        try:
            WebDriverWait(driver, 10).until(
                lambda d: len(d.find_element(By.TAG_NAME, "body").text.strip()) > 200
            )
        except Exception:
            time.sleep(4)
        body = driver.find_element(By.TAG_NAME, "body")
        return clean_text(body.text)
    except Exception:
        return ""


def matches_category(text: str, keywords: list[str]) -> bool:
    text_lower = text.lower()
    return any(kw in text_lower for kw in keywords)


# ── Qdrant ─────────────────────────────────────────────────────────────────

def ensure_payload_index(client: QdrantClient):
    from qdrant_client.models import PayloadSchemaType
    try:
        client.create_payload_index(
            collection_name=COLLECTION_NAME,
            field_name="doc_id",
            field_schema=PayloadSchemaType.KEYWORD,
        )
    except Exception:
        pass


def doc_exists(client: QdrantClient, doc_id: str) -> bool:
    results, _ = client.scroll(
        collection_name=COLLECTION_NAME,
        scroll_filter=Filter(
            must=[FieldCondition(key="doc_id", match=MatchValue(value=doc_id))]
        ),
        limit=1,
        with_payload=False,
        with_vectors=False,
        timeout=30,
    )
    return len(results) > 0


def index_batch(client: QdrantClient, model: SentenceTransformer, batch: list[dict]):
    texts = [f"passage: {item['text']}" for item in batch]
    embeddings = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
    points = [
        PointStruct(
            id=str(uuid.uuid4()),
            vector=embeddings[i].tolist(),
            payload={
                "doc_id":    item["doc_id"],
                "title":     item["title"],
                "text":      item["text"],
                "doc_type":  item["doc_type"],
                "date":      item["date"],
                "chunk_idx": item["chunk_idx"],
                "source":    "kad.arbitr.ru",
                "category":  item["category"],
            },
        )
        for i, item in enumerate(batch)
    ]
    client.upsert(collection_name=COLLECTION_NAME, points=points)


# ── Браузер (держим живым для всех запросов) ──────────────────────────────

def init_driver(headless: bool = False) -> uc.Chrome:
    """
    Открывает Chrome, проходит DDoS Guard и возвращает живой драйвер.
    Все API-запросы идут через этот же браузер (настоящий TLS Chrome).

    headless=True убирает видимое окно ОС — обязательно при параллельном
    запуске нескольких терминалов: ввод дат идёт через ActionChains.send_keys()
    без привязки к элементу, а это зависит от фокуса окна на уровне ОС.
    Два видимых окна одновременно → фокус может достаться не тому процессу,
    и ввод улетает в чужое окно (воспроизведено 2026-10-01, см. memory).
    """
    print(f"Инициализирую браузер (обход DDoS Guard){', headless' if headless else ''}...")
    os.makedirs(PDF_DOWNLOAD_DIR, exist_ok=True)

    options = uc.ChromeOptions()
    options.add_argument("--no-sandbox")
    options.add_argument("--window-size=1280,720")
    # PDF скачивается как файл, а не открывается во вьюере
    options.add_experimental_option("prefs", {
        "plugins.always_open_pdf_externally": True,
        "download.default_directory": PDF_DOWNLOAD_DIR,
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
    })

    # Явно определяем major-версию установленного Chrome и передаём как
    # version_main — по умолчанию undetected_chromedriver иногда подтягивает
    # САМЫЙ СВЕЖИЙ chromedriver (если он уже вышел), который может опережать
    # ещё не обновившийся локально Chrome — тогда SessionNotCreatedException
    # ("This version of ChromeDriver only supports Chrome version N+1").
    # Воспроизведено и подтверждено 2026-10-07 (Chrome 154, chromedriver 155).
    version_main = None
    try:
        for chrome_path in (
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ):
            if os.path.exists(chrome_path):
                out = subprocess.run(
                    ["powershell", "-Command",
                     f"(Get-Item '{chrome_path}').VersionInfo.ProductVersion"],
                    capture_output=True, text=True, timeout=10,
                ).stdout.strip()
                if out:
                    version_main = int(out.split(".")[0])
                    break
    except Exception:
        version_main = None  # не удалось определить — пусть uc решает сам

    # headless передаём именно через kwarg uc.Chrome (не --headless в options):
    # undetected_chromedriver сам накладывает доп. патчи против детекта headless,
    # обычный --headless светится сильнее и может не пройти DDoS Guard.
    driver = uc.Chrome(options=options, use_subprocess=True, headless=headless,
                        version_main=version_main)
    # Без явного таймаута driver.get() может зависнуть на дефолте Chrome/Selenium
    # (порядка нескольких минут) при зависшем сетевом запросе — воспроизведено
    # и подтверждено пользователем вживую 2026-10-01 (зависание >5 минут на
    # скачивании PDF). 30с достаточно с запасом даже для больших PDF (обычный
    # цикл ожидания файла в get_text_from_ras и так ограничен 30с отдельно).
    driver.set_page_load_timeout(30)
    driver.get("https://kad.arbitr.ru/")
    time.sleep(4)  # ждём прохождения JS-вызова DDoS Guard

    cookies = [c["name"] for c in driver.get_cookies()]
    print(f"  Браузер готов, cookies: {cookies}\n")
    return driver


# ── kad.arbitr.ru — поиск через UI (перехват XHR) ─────────────────────────

# JavaScript: перехват XHR + fetch для capture ответов API
_XHR_INTERCEPT = """
window._kadSearchResult = null;
window._kadDocsResult = null;
window._kadRequests = [];
(function() {
    // --- XHR ---
    const origOpen = XMLHttpRequest.prototype.open;
    const origSend = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function(method, url) {
        this._kadUrl = url;
        window._kadRequests.push('XHR:' + url);
        return origOpen.apply(this, arguments);
    };
    XMLHttpRequest.prototype.send = function() {
        const xhr = this;
        xhr.addEventListener('load', function() {
            if (xhr._kadUrl && xhr._kadUrl.includes('SearchInstances')) {
                window._kadSearchRaw = {status: xhr.status, text: xhr.responseText.substring(0, 3000)};
                window._kadSearchResult = {_html: xhr.responseText};
            }
            if (xhr._kadUrl && xhr._kadUrl.includes('InstanceDocuments')) {
                try { window._kadDocsResult = JSON.parse(xhr.responseText); } catch(e) {}
            }
        });
        return origSend.apply(this, arguments);
    };
    // --- fetch ---
    const origFetch = window.fetch;
    window.fetch = function(url, opts) {
        const urlStr = (url || '').toString();
        window._kadRequests.push('fetch:' + urlStr);
        return origFetch.apply(this, arguments).then(function(r) {
            const clone = r.clone();
            if (urlStr.includes('SearchInstances')) {
                clone.json().then(function(d) { window._kadSearchResult = d; }).catch(function(){});
            }
            if (urlStr.includes('InstanceDocuments')) {
                clone.json().then(function(d) { window._kadDocsResult = d; }).catch(function(){});
            }
            return r;
        });
    };
})();
"""


def _inject_intercept(driver: uc.Chrome):
    driver.execute_script(_XHR_INTERCEPT)


def _wait_for_result(driver: uc.Chrome, var: str, timeout: int = 20) -> dict:
    """Ждёт пока window._kad* заполнится ненулевым значением."""
    for _ in range(timeout * 2):
        val = driver.execute_script(f"return window.{var};")
        if val is not None:
            driver.execute_script(f"window.{var} = null;")  # сброс для следующего вызова
            return val
        time.sleep(0.5)
    return {}


def _dismiss_popups(driver: uc.Chrome):
    """
    Закрывает модалки/баннеры на kad.arbitr.ru. Сайт-объявление ("Уважаемые
    пользователи!") общее для kad.arbitr.ru и ras.arbitr.ru — на скриншоте
    2026-10-01 подтверждено, что CSS-скрытие по классу его не закрывает
    (видимо, у фона-подложки другой класс), поэтому добавлены Escape + явный
    клик по кнопке закрытия, как уже проверено рабочим для _ras_dismiss_modals().
    """
    try:
        from selenium.webdriver.common.keys import Keys as _Keys
        driver.find_element(By.TAG_NAME, "body").send_keys(_Keys.ESCAPE)
        time.sleep(0.3)
    except Exception:
        pass
    try:
        driver.execute_script("""
            document.querySelectorAll(
                '[class*="popup"], [class*="modal"], [class*="notification"], [class*="promo"]'
            ).forEach(el => { el.style.display = 'none'; el.style.visibility = 'hidden'; });
        """)
    except Exception:
        pass
    for sel in [
        "button.close", ".modal .close", ".modal-close",
        "button[aria-label='Close']", "button[aria-label='Закрыть']",
        "[class*='promo_notification'] [class*='close']",
    ]:
        try:
            for el in driver.find_elements(By.CSS_SELECTOR, sel):
                if el.is_displayed():
                    driver.execute_script("arguments[0].click();", el)
                    time.sleep(0.2)
        except Exception:
            pass
    time.sleep(0.3)


def _parse_rows_from_dom(driver: uc.Chrome) -> list[dict]:
    """
    Читает строки результатов из DOM.
    Структура: <td class="num"><a class="num_case" href=".../Card/{GUID}">А81-xxx</a>
               <td class="court"><div title="АС ...">
    """
    try:
        WebDriverWait(driver, 30).until(
            lambda d: d.find_elements(By.CSS_SELECTOR, "a.num_case")
        )
    except Exception:
        # Отладка: что вообще на странице
        url = driver.current_url
        title = driver.title
        td_num = driver.find_elements(By.CSS_SELECTOR, "td.num")
        print(f"  [debug] wait timeout. URL={url} title={title!r} td.num={len(td_num)}")
        return []

    links = driver.find_elements(By.CSS_SELECTOR, "a.num_case")
    cases = []
    for link in links:
        href = link.get_attribute("href") or ""
        # GUID из URL: /Card/{guid}
        m = re.search(r"/Card/([0-9a-f-]{36})", href)
        case_id = m.group(1) if m else ""
        case_number = link.text.strip()

        # Поднимаемся к <tr> для получения суда и даты
        try:
            row = link.find_element(By.XPATH, "ancestor::tr[1]")
            court_els = row.find_elements(By.CSS_SELECTOR, "td.court div[title]")
            court = ""
            for cel in court_els:
                val = (cel.get_attribute("title") or cel.text.strip()).strip()
                # Название суда длиннее и содержит «суд», «АС» или «инстанц»
                if any(w in val.lower() for w in ["суд", " ас ", "инстанц"]) or len(val) > 25:
                    court = val[:60]
                    break
            if not court and court_els:
                court = (court_els[0].get_attribute("title") or "")[:60]
        except Exception:
            court = ""

        try:
            date_el = link.find_element(By.XPATH,
                "ancestor::td[1]//div[@title]")
            date_raw = date_el.get_attribute("title") or ""
            date_val = date_raw[:10]  # "22.09.2026"
        except Exception:
            date_val = ""

        if case_number:
            cases.append({
                "CaseId":     case_id,
                "CaseNumber": case_number,
                "CourtName":  court,
                "DateStart":  date_val,
            })
    return cases


def _get_total_count(driver: uc.Chrome) -> int:
    try:
        el = driver.find_element(By.CSS_SELECTOR,
            ".b-pager-total, [class*='total'], [class*='count']")
        nums = re.findall(r"\d[\d ]*", el.text)
        if nums:
            return int(nums[0].replace(" ", ""))
    except Exception:
        pass
    return 0


# Месяц→номер для русских названий в календаре kad.arbitr.ru
_RU_MONTHS = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4,
    "май": 5, "мая": 5, "июн": 6, "июл": 7, "август": 8,
    "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}


def _cal_current(driver) -> tuple[int, int]:
    """Читает текущий месяц и год из открытого календаря. Возвращает (month, year)."""
    try:
        # Ищем любой видимый элемент, содержащий месяц и 4-значный год
        candidates = driver.find_elements(By.XPATH,
            "//*[contains(text(),'2024') or contains(text(),'2025') or "
            "    contains(text(),'2026') or contains(text(),'2023')]"
            "[contains(text(),'январ') or contains(text(),'феврал') or "
            " contains(text(),'март') or contains(text(),'апрел') or "
            " contains(text(),'май') or contains(text(),'июн') or "
            " contains(text(),'июл') or contains(text(),'август') or "
            " contains(text(),'сентябр') or contains(text(),'октябр') or "
            " contains(text(),'ноябр') or contains(text(),'декабр')]")
        for el in candidates:
            if not el.is_displayed():
                continue
            txt = el.text.strip().lower()
            ym = re.search(r"\d{4}", txt)
            if not ym:
                continue
            year = int(ym.group())
            month = next((v for k, v in _RU_MONTHS.items() if k in txt), None)
            if month and 2000 <= year <= 2030:
                return month, year
    except Exception:
        pass
    return None, None


def _cal_navigate(driver, target_month: int, target_year: int):
    """Кликает ◄/► пока календарь не покажет нужный месяц/год."""
    for _ in range(50):
        m, y = _cal_current(driver)
        if m is None:
            break
        if (y, m) == (target_year, target_month):
            return
        if (y, m) > (target_year, target_month):
            btn = driver.find_element(By.CSS_SELECTOR,
                ".ui-datepicker-prev, [class*='prev'], [data-action='prev'], [title*='Пред'],"
                " a.b-calendar-btn-prev")
        else:
            btn = driver.find_element(By.CSS_SELECTOR,
                ".ui-datepicker-next, [class*='next'], [data-action='next'], [title*='След'],"
                " a.b-calendar-btn-next")
        driver.execute_script("arguments[0].click();", btn)
        time.sleep(0.3)


def _cal_click_day(driver, day: int):
    """Кликает на день в открытом календаре."""
    day_el = driver.find_element(By.XPATH,
        f"//td[not(contains(@class,'disabled'))][.//a[normalize-space(text())='{day}']]//a")
    driver.execute_script("arguments[0].click();", day_el)
    time.sleep(0.3)


def _set_date_range(driver, date_from_str: str, date_to_str: str):
    """
    Выставляет диапазон дат: JS-клик → Escape в body → JS focus → ActionChains ввод.
    date_from_str и date_to_str в формате 'YYYY-MM-DD'.
    """
    from selenium.webdriver.common.action_chains import ActionChains
    from selenium.webdriver.common.keys import Keys as _Keys

    df_txt = datetime.strptime(date_from_str, "%Y-%m-%d").strftime("%d.%m.%Y")
    dt_txt = datetime.strptime(date_to_str,   "%Y-%m-%d").strftime("%d.%m.%Y")

    _DEBUG_DATES = os.environ.get("KAD_DEBUG_DATES") == "1"

    # Явное ожидание появления ОБОИХ полей в DOM — раньше был мгновенный
    # find_elements без ожидания: если JS-форма ещё не отрендерилась (под
    # нагрузкой от параллельного запуска другого терминала фиксированной паузы
    # в init_driver() может не хватать), поиск возвращал <2 элементов и функция
    # молча завершалась, вообще не трогая поля — воспроизведено и подтверждено
    # пользователем вживую 2026-10-01 (второй терминал не вводил даты вообще).
    try:
        WebDriverWait(driver, 15).until(
            lambda d: len(d.find_elements(By.CSS_SELECTOR,
                "input[placeholder*='дд'], input[placeholder*='ДД']")) >= 2
        )
    except Exception:
        if _DEBUG_DATES:
            print("    [debug-fill] поля дат не появились за 15с — выхожу")
        return

    date_inputs = driver.find_elements(By.CSS_SELECTOR,
        "input[placeholder*='дд'], input[placeholder*='ДД']")
    if len(date_inputs) < 2:
        return

    def fill_input(inp, value):
        def _v(label):
            if _DEBUG_DATES:
                try:
                    print(f"    [debug-fill] {label}: value={inp.get_attribute('value')!r} active={driver.switch_to.active_element == inp}")
                except Exception as e:
                    print(f"    [debug-fill] {label}: <error reading state: {e}>")

        driver.execute_script("arguments[0].click();", inp)
        time.sleep(0.4)
        _v("after click")
        driver.find_element(By.TAG_NAME, "body").send_keys(_Keys.ESCAPE)
        time.sleep(0.2)
        _v("after escape")
        driver.execute_script("arguments[0].focus();", inp)
        time.sleep(0.1)
        _v("after focus")
        ActionChains(driver).key_down(_Keys.CONTROL).send_keys("a").key_up(_Keys.CONTROL).perform()
        time.sleep(0.1)
        _v("after ctrl+a")
        ActionChains(driver).send_keys(_Keys.DELETE).perform()
        time.sleep(0.1)
        _v("after delete")
        ActionChains(driver).send_keys(value).perform()
        time.sleep(0.3)
        _v("after type")
        ActionChains(driver).send_keys(_Keys.TAB).perform()
        time.sleep(0.3)
        _v("after tab")

    try:
        fill_input(date_inputs[0], df_txt)
    except Exception as e:
        if _DEBUG_DATES:
            print(f"    [debug-fill] поле 'от' бросило исключение: {e!r}")
    try:
        fill_input(date_inputs[1], dt_txt)
    except Exception as e:
        if _DEBUG_DATES:
            print(f"    [debug-fill] поле 'до' бросило исключение: {e!r}")


def _is_rate_limited(*texts: str) -> bool:
    """
    Признак бана/троттлинга (429/451) ИЛИ перегрузки бэкенда (503) в тексте
    страницы. 503 подтверждён пользователем вживую 2026-10-01 — настоящая
    серверная ошибка ("503 Service Unavailable / No server is available to
    handle this request") на конкретном деле, которое до этого молча
    считалось "не скачалось за 30с"/"деградация сессии". Это не блокировка
    по IP, а перегрузка сайта — тоже стоит делать паузу перед повтором.
    """
    joined = " ".join(t or "" for t in texts).lower()
    return (
        "429" in joined or "451" in joined or "503" in joined
        or "ошибка получения информации" in joined
        or "service unavailable" in joined
        or "no server is available" in joined
    )


MAX_RATE_LIMIT_RETRIES = 3
MAX_DATE_DEGRADE_RETRIES = 2
DATE_DEGRADE_PAUSE = 20.0


def search_cases(driver: uc.Chrome, case_type: str, date_from: str, date_to: str, page: int = 1,
                  _retry: int = 0, _date_retry: int = 0) -> dict:
    """Поиск через UI: клик Найти + чтение DOM."""
    if driver.current_url.rstrip("/") != "https://kad.arbitr.ru":
        try:
            driver.get("https://kad.arbitr.ru/")
        except Exception as e:
            print(f"  [debug] driver.get() завис/оборвался по таймауту: {e!r}")
        # Ждём реальной готовности формы (поля дат в DOM), а не плоский sleep —
        # _dismiss_popups() ниже шлёт Escape, а Escape во время ЕЩЁ идущей
        # загрузки страницы в Chrome может оборвать её (как кнопка "Стоп"),
        # и тогда JS-форма с датами не успевает дорендериться. Воспроизведено
        # и подтверждено пользователем вживую 2026-10-01 (стабильно ломалось
        # именно на странице 2+, где эта ветка перезагрузки и срабатывает).
        try:
            WebDriverWait(driver, 15).until(
                lambda d: len(d.find_elements(By.CSS_SELECTOR,
                    "input[placeholder*='дд'], input[placeholder*='ДД']")) >= 2
            )
        except Exception:
            time.sleep(3)

    _dismiss_popups(driver)

    # Сначала нажимаем Escape, чтобы закрыть любой календарь/выпадалку
    try:
        from selenium.webdriver.common.keys import Keys
        driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
        time.sleep(0.3)
    except Exception:
        pass

    if case_type == "Б":
        try:
            tab = driver.find_element(By.XPATH, "//a[contains(text(),'Банкрот')]")
            tab.click()
            time.sleep(0.5)
        except Exception:
            pass

    # Заполняем даты через навигацию по календарю
    _set_date_range(driver, date_from[:10], date_to[:10])
    _expected_from = datetime.strptime(date_from[:10], "%Y-%m-%d").strftime("%d.%m.%Y")
    _expected_to   = datetime.strptime(date_to[:10],   "%Y-%m-%d").strftime("%d.%m.%Y")
    try:
        _inps = driver.find_elements(By.CSS_SELECTOR, "input[placeholder*='дд']")
        _v0 = _inps[0].get_attribute("value") if _inps else "?"
        _v1 = _inps[1].get_attribute("value") if len(_inps) > 1 else "?"
        print(f"  [debug] dates set: от='{_v0}' до='{_v1}'")
    except Exception:
        _v0 = _v1 = "?"

    # Деградация сессии: поля дат не выставляются, но сайт при этом НЕ
    # показывает ошибку — честно отдаёт 25 результатов ДЕФОЛТНОГО диапазона
    # (без фильтра), и код раньше этого не замечал вообще. Похоже на то же
    # явление, что у pravo.gov.ru — сайт деградирует функциональность после
    # нескольких циклов "kad.arbitr.ru → ras.arbitr.ru → снова kad.arbitr.ru"
    # (характерный паттерн бота), без явного 429/451. Воспроизведено и
    # подтверждено пользователем вживую 2026-10-01 (один из двух параллельных
    # терминалов стабильно не восстанавливался сам после первого такого сбоя).
    if _v0 != _expected_from or _v1 != _expected_to:
        try:
            _body_503 = driver.find_element(By.TAG_NAME, "body").text[:200]
        except Exception:
            _body_503 = ""
        _is_503 = _is_rate_limited(_body_503)
        _pause = DELAY_RETRY if _is_503 else DATE_DEGRADE_PAUSE
        if _date_retry < MAX_DATE_DEGRADE_RETRIES:
            label = f"503/перегрузка бэкенда ({_body_503[:100]!r})" if _is_503 else "деградация сессии (причина не ясна)"
            print(f"  ⚠ {label}: даты не применились (ожидали "
                  f"'{_expected_from}'/'{_expected_to}', получили '{_v0}'/'{_v1}'). "
                  f"Жёсткий сброс страницы + пауза {_pause:.0f}с "
                  f"({_date_retry + 1}/{MAX_DATE_DEGRADE_RETRIES})...")
            try:
                driver.get("about:blank")
                time.sleep(1.0)
            except Exception:
                pass
            time.sleep(_pause)
            try:
                driver.get("https://kad.arbitr.ru/")
                WebDriverWait(driver, 15).until(
                    lambda d: len(d.find_elements(By.CSS_SELECTOR,
                        "input[placeholder*='дд'], input[placeholder*='ДД']")) >= 2
                )
            except Exception as e:
                print(f"  [debug] сброс страницы не помог: {e!r}")
            return search_cases(driver, case_type, date_from, date_to, page=page,
                                 _retry=_retry, _date_retry=_date_retry + 1)
        print(f"  ⚠ Деградация сессии не снимается после {MAX_DATE_DEGRADE_RETRIES} "
              f"попыток сброса — останавливаю прогон на этой странице.")
        return {}

    # Повторно закрываем попапы — баннер-объявление может появиться с задержкой
    # уже после первого dismiss в начале функции (см. _dismiss_popups).
    _dismiss_popups(driver)

    # Клик Найти через JS (обходит перекрывающие элементы)
    try:
        btn = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located(
                (By.XPATH, "//button[contains(text(),'Найти')] | //input[@value='Найти']")
            )
        )
        driver.execute_script("arguments[0].click();", btn)
    except Exception as e:
        print(f"  [debug] кнопка не найдена: {e}")
        return {}

    # Ждём начала загрузки результатов
    time.sleep(5)
    _n_td = len(driver.find_elements(By.CSS_SELECTOR, "td.num"))
    _n_a = len(driver.find_elements(By.CSS_SELECTOR, "a.num_case"))
    # Ищем любые сообщения об ошибках или состоянии
    _err_els = driver.find_elements(By.CSS_SELECTOR,
        "[class*='error'], [class*='empty'], [class*='message'], [class*='notice']")
    _err_texts = [e.text.strip()[:80] for e in _err_els if e.text.strip()][:3]
    print(f"  [debug] после sleep 5s: td.num={_n_td}, a.num_case={_n_a}, msg={_err_texts}")

    # Реальная проверка на бан/троттлинг — раньше "429" только печаталась в debug
    # и игнорировалась, скрипт продолжал долбить на той же скорости.
    if _is_rate_limited(*_err_texts):
        if _retry < MAX_RATE_LIMIT_RETRIES:
            print(f"  ⚠ Обнаружен признак блокировки (429/451). Пауза {DELAY_RETRY:.0f}с "
                  f"перед повтором ({_retry + 1}/{MAX_RATE_LIMIT_RETRIES})...")
            time.sleep(DELAY_RETRY)
            return search_cases(driver, case_type, date_from, date_to, page=page, _retry=_retry + 1)
        print(f"  ⚠ Блокировка не снимается после {MAX_RATE_LIMIT_RETRIES} попыток — "
              f"останавливаю прогон. Подождите (обычно 1-24ч) и запустите заново.")
        return {}

    # Страница > 1: кликаем в пагинаторе
    if page > 1:
        try:
            time.sleep(2)
            pg = driver.find_element(By.XPATH,
                f"//a[contains(@class,'pager') and normalize-space(text())='{page}']")
            pg.click()
            time.sleep(2)
        except Exception:
            pass

    cases = _parse_rows_from_dom(driver)
    total = _get_total_count(driver)
    return {"Result": {"Items": cases, "TotalCount": total}}


def get_case_documents(driver: uc.Chrome, case_id: str) -> list[dict]:
    """
    Получает документы по GUID дела.
    Открывает /Card/{case_id}, затем инжектирует перехватчик XHR
    и ищет ссылки на PDF в DOM.
    """
    driver.get(f"{KAD_URL}/Card/{case_id}")
    time.sleep(1)
    # Инжектируем ПОСЛЕ навигации, чтобы не потерять JS при переходе
    _inject_intercept(driver)
    time.sleep(3)

    # Сначала пробуем XHR
    data = _wait_for_result(driver, "_kadDocsResult", timeout=8)
    if data:
        print(f"    [debug] XHR docs: {str(data)[:400]}")
        result = data.get("Result", [])
        items = result.get("Items", []) if isinstance(result, dict) else (result or [])
        if items:
            return items

    # Fallback: ищем прямые ссылки на PDF в DOM
    docs = []
    try:
        # Debug: все ссылки с GUID
        all_a = driver.find_elements(By.XPATH, "//a[@href]")
        all_hrefs = [(a.get_attribute("href") or "") for a in all_a]
        guid_hrefs = [h for h in all_hrefs if re.search(r"[0-9a-f]{8}-[0-9a-f]{4}", h)]
        non_card = [h for h in guid_hrefs if "Card/" not in h]
        print(f"    [debug] ссылки с GUID (не Card): {non_card[:5]}")
    except Exception:
        pass

    # Ищем прямые ссылки на PDF-файлы (/Kad/PdfDocument/{caseId}/{docId}/filename.pdf)
    try:
        doc_links = driver.find_elements(By.XPATH,
            "//a[contains(@href, 'PdfDocument') and contains(@href, '.pdf')]")
        for a in doc_links:
            href = a.get_attribute("href") or ""
            # Формат: /Kad/PdfDocument/{caseId}/{docId}/{filename}.pdf
            m = re.search(r"PdfDocument/[0-9a-f-]+/([0-9a-f-]{36})/([^/]+\.pdf)", href)
            if m:
                docs.append({
                    "DocId":   m.group(1),
                    "Title":   m.group(2),
                    "PdfUrl":  href,
                })
    except Exception:
        pass

    if not docs:
        # Ищем PublishReport (html-страница с текстом решения)
        try:
            pub_links = driver.find_elements(By.XPATH,
                "//a[contains(@href, 'PublishReport')]")
            for a in pub_links:
                href = a.get_attribute("href") or ""
                m_inst = re.search(r"instanceId=([0-9a-f-]{36})", href)
                if m_inst:
                    docs.append({
                        "DocId": m_inst.group(1),
                        "Title": a.text.strip() or "Решение",
                        "Url":   href,
                    })
        except Exception:
            pass

    return docs


def download_pdf(driver: uc.Chrome, doc_id: str, pdf_url: str = "") -> bytes:
    """
    Скачивает PDF через fetch() внутри браузера (обходит TLS fingerprinting).
    Если не получилось — пробует через requests с cookies.
    """
    import base64

    urls = []
    if pdf_url:
        urls.append(pdf_url)
    if doc_id:
        urls += [
            f"{KAD_URL}/Kad/PdfDocument/{doc_id}",
            f"{KAD_URL}/Kad/Document/{doc_id}",
        ]

    for url in urls:
        # Сначала через fetch() в браузере
        try:
            result = driver.execute_async_script("""
                var url = arguments[0];
                var callback = arguments[arguments.length - 1];
                fetch(url, {credentials: 'include'})
                    .then(function(r) {
                        if (!r.ok) { callback(null); return; }
                        return r.blob();
                    })
                    .then(function(blob) {
                        var reader = new FileReader();
                        reader.onload = function() {
                            var b64 = reader.result.split(',')[1];
                            callback(b64);
                        };
                        reader.readAsDataURL(blob);
                    })
                    .catch(function() { callback(null); });
            """, url)
            if result:
                data = base64.b64decode(result)
                if data[:4] == b'%PDF' or len(data) > 10000:
                    return data
        except Exception:
            pass

        # Fallback: requests с cookies
        try:
            browser_cookies = driver.get_cookies()
            session = requests.Session()
            for c in browser_cookies:
                session.cookies.set(c["name"], c["value"],
                                    domain=c.get("domain", ".kad.arbitr.ru"))
            r = session.get(url, headers=HEADERS, timeout=60)
            if r.status_code == 200 and r.content[:4] == b'%PDF':
                return r.content
        except Exception:
            pass

    return b""


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
):
    cat = CATEGORIES[category_key]
    _run_start = time.time()
    print(f"\nКатегория:  {cat['label']}")
    print(f"Период:     {date_from[:10]} — {date_to[:10]}")
    print(f"Лимит:      {limit} дел")
    print(f"Режим:      {'probe (только поиск)' if probe else 'dry-run (без индексации)' if dry_run else 'полная индексация'}")
    if shard is not None:
        print(f"Шард:       {shard[0]}/{shard[1]}")
    print()

    driver = init_driver(headless=headless)

    try:
      # ── Probe: только проверить API и показать первые N дел ────────────────
      if probe:
        print("Проверяю API kad.arbitr.ru...")
        data = search_cases(driver, cat["case_type"], date_from, date_to, page=1)
        if not data:
            print("ОШИБКА: API не ответил или вернул пустой ответ.")
            return

        result = data.get("Result", {})
        total = result.get("TotalCount", 0)
        items = result.get("Items", [])

        print(f"API OK — всего дел в базе: {total:,}")
        print(f"\nПервые {min(len(items), 5)} дел:\n")
        for item in items[:5]:
            print(f"  {item.get('CaseNumber', '—')}  |  {item.get('CourtName', '—')[:50]}")
            print(f"    CaseId: {item.get('CaseId', '—')}")
            print(f"    Дата:   {item.get('DateStart', '—')[:10]}")
            print()

        if items:
            first_id     = items[0].get("CaseId")
            first_number = items[0].get("CaseNumber", "")
            print(f"Пробую get_text_from_ras для {first_number}...")
            text = get_text_from_ras(driver, first_number)
            print(f"  Текст: {len(text)} символов")
            if text:
                print(f"  Начало: {text[:500]!r}")
            else:
                # Дополнительная отладка: что вообще нашлось на ras.arbitr.ru
                print("  Текст не получен — проверяю структуру страницы...")
                try:
                    driver.get(RAS_URL)
                    time.sleep(3)
                    inputs = driver.find_elements(By.TAG_NAME, "input")
                    for i, inp in enumerate(inputs[:10]):
                        ph = inp.get_attribute("placeholder") or ""
                        tp = inp.get_attribute("type") or ""
                        print(f"    input[{i}] type={tp!r} placeholder={ph!r}")
                    btns = driver.find_elements(By.TAG_NAME, "button")
                    for b in btns[:5]:
                        print(f"    button: {b.text[:40]!r}")
                except Exception as dbg_e:
                    print(f"  [debug] ошибка при отладке ras: {dbg_e}")
        return

      # ── Полный/dry-run прогон ───────────────────────────────────────────────
      if not dry_run:
        print("Подключаюсь к Qdrant...")
        client = QdrantClient(url=QDRANT_URL, timeout=60)
        ensure_payload_index(client)
        print("Загружаю модель эмбеддинга...")
        import torch
        device = "cuda" if torch.cuda.is_available() else "cpu"
        print(f"  Устройство: {device}")
        model = SentenceTransformer(EMBEDDING_MODEL, device=device)
      else:
        client = model = None

      stats = {
        "processed": 0, "indexed": 0, "skipped_dup": 0,
        "skipped_no_pdf": 0, "skipped_no_text": 0,
        "skipped_category": 0, "chunks": 0,
      }
      batch = []
      page = 1

      while stats["processed"] < limit:
        data = search_cases(driver, cat["case_type"], date_from, date_to, page=page)
        if not data:
            print("Поиск вернул пустой ответ, останавливаюсь.")
            break

        items = data.get("Result", {}).get("Items", [])
        if not items:
            print("Больше дел нет.")
            break

        for item in items:
            if stats["processed"] >= limit:
                break

            case_number = item.get("CaseNumber", "—")
            case_id     = item.get("CaseId", "")
            court       = item.get("CourtName", "")[:60]
            date_raw    = str(item.get("DateStart", ""))[:10]

            # Шардинг для параллельных терминалов: без него kad.arbitr.ru не
            # фильтрует по категории на сайте, и несколько терминалов на один
            # период/case_type читают один и тот же список дел целиком —
            # дублируют работу и сталкиваются на общих файлах. Хэш номера дела
            # детерминированно делит список без координации между процессами.
            if shard is not None:
                shard_index, shard_total = shard
                case_hash = int(hashlib.md5(case_number.encode("utf-8")).hexdigest(), 16)
                if case_hash % shard_total != shard_index:
                    continue

            stats["processed"] += 1
            print(f"  [{stats['processed']:04d}] {case_number}  |  {court}")

            # DELAY_CASE должна сработать на КАЖДОЕ дело независимо от исхода
            # (раньше стояла после stats["indexed"] += 1 и пропускалась в dry-run
            # и на любом skip — реальная причина повторных 429, см. память проекта)
            try:
                # Дедупликация
                if not dry_run and doc_exists(client, case_number):
                    stats["skipped_dup"] += 1
                    print(f"         → дубль, пропускаю")
                    continue

                # Получаем текст решения с ras.arbitr.ru
                text = get_text_from_ras(driver, case_number, case_id=case_id)

                if not text:
                    stats["skipped_no_pdf"] += 1
                    print(f"         → текст не найден на ras.arbitr.ru")
                    continue
                if len(text) < 200:
                    stats["skipped_no_text"] += 1
                    print(f"         → текст слишком короткий ({len(text)} симв.)")
                    continue

                # Фильтр по категории
                if not matches_category(text, cat["keywords"]):
                    stats["skipped_category"] += 1
                    print(f"         → не соответствует категории '{cat['label']}'")
                    continue

                print(f"         → OK, текст {len(text)} симв.")

                if dry_run:
                    stats["indexed"] += 1
                    continue

                # Чанкуем и добавляем в батч
                chunks = chunk_text(text)
                for idx, chunk in enumerate(chunks):
                    batch.append({
                        "doc_id":    case_number,
                        "title":     f"{case_number} — {court}",
                        "text":      chunk,
                        "doc_type":  "Судебное решение",
                        "date":      date_raw,
                        "chunk_idx": idx,
                        "category":  category_key,
                    })
                    stats["chunks"] += 1

                if len(batch) >= BATCH_SIZE:
                    index_batch(client, model, batch)
                    batch = []

                stats["indexed"] += 1
            finally:
                time.sleep(DELAY_CASE)

        page += 1
        time.sleep(DELAY_PAGE)

      # Последний батч
      if batch and not dry_run:
        index_batch(client, model, batch)

      elapsed = time.time() - _run_start
      per_case = elapsed / stats["processed"] if stats["processed"] else 0
      print(f"\n{'='*60}")
      print(f"ИТОГИ [{cat['label']}]")
      print(f"  Обработано дел:          {stats['processed']:,}")
      print(f"  Проиндексировано:        {stats['indexed']:,}")
      print(f"  Пропущено (дубли):       {stats['skipped_dup']:,}")
      print(f"  Пропущено (нет PDF):     {stats['skipped_no_pdf']:,}")
      print(f"  Пропущено (нет текста):  {stats['skipped_no_text']:,}")
      print(f"  Пропущено (не та катег.): {stats['skipped_category']:,}")
      if not dry_run:
        print(f"  Загружено чанков:        {stats['chunks']:,}")
      print(f"\n  Elapsed:  {elapsed:.0f}с  ({per_case:.1f}с/дело)")
      if per_case > 0:
        for n in [1000, 2000, 6000]:
            h = per_case * n / 3600
            print(f"  Прогноз {n:,} дел: {h:.1f} ч")

    finally:
        driver.quit()


def main():
    parser = argparse.ArgumentParser(description="Индексация kad.arbitr.ru")
    parser.add_argument(
        "--category",
        choices=list(CATEGORIES.keys()),
        default="tax",
        help="Категория дел (tax / contract / corporate / bankruptcy)",
    )
    parser.add_argument(
        "--limit", type=int, default=10,
        help="Максимум дел для обработки (по умолчанию 10 для теста)",
    )
    parser.add_argument(
        "--probe", action="store_true",
        help="Только проверить API: показать первые дела без скачивания",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Скачать и распознать, но не индексировать в Qdrant",
    )
    parser.add_argument(
        "--date-from", default=None,
        help="Начало периода YYYY-MM-DD (по умолчанию 3 года назад)",
    )
    parser.add_argument(
        "--date-to", default=None,
        help="Конец периода YYYY-MM-DD (по умолчанию сегодня)",
    )
    parser.add_argument(
        "--headless", action="store_true",
        help="Без видимого окна браузера. НЕ использовать на kad.arbitr.ru — "
             "ломает JS-валидацию формы поиска (см. память проекта, 2026-10-01).",
    )
    parser.add_argument(
        "--shard", default=None,
        help="N/M — делит список дел по хэшу номера на M частей, обрабатывает "
             "только часть N (0..M-1). Для параллельных терминалов на один и "
             "тот же период/категорию: --shard 0/2 и --shard 1/2 не пересекаются.",
    )
    args = parser.parse_args()

    today = datetime.now()
    date_to   = args.date_to   or today.strftime("%Y-%m-%dT23:59:59")
    date_from = args.date_from or (today - timedelta(days=3*365)).strftime("%Y-%m-%dT00:00:00")

    shard = None
    if args.shard:
        idx_str, total_str = args.shard.split("/")
        shard = (int(idx_str), int(total_str))

    run(
        category_key=args.category,
        limit=args.limit,
        date_from=date_from,
        date_to=date_to,
        probe=args.probe,
        dry_run=args.dry_run,
        headless=args.headless,
        shard=shard,
    )


if __name__ == "__main__":
    main()
