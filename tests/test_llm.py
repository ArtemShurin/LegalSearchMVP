"""
Standalone тест LLM-генерации ответа (без FastAPI и бека).

Этапы:
  1. Query rewriting — переформулировать запрос в юридические термины
  2. Генерация ответа — суммировать найденные чанки со ссылками

Запуск:
    # Интерактивный режим (вводишь запрос вручную, вставляешь чанки из test_pipeline.py):
    python tests/test_llm.py

    # Быстрый тест с захардкоженными примерами:
    python tests/test_llm.py --demo

    # Полный pipeline: embed → retrieve → rerank → LLM:
    python tests/test_llm.py --full "сосед затопил квартиру кто платит"
"""

import argparse
import sys
import io
import json
import time
import urllib.request
import urllib.error

# Принудительно UTF-8 для вывода на Windows (cp1251 не поддерживает стрелки и спецсимволы)
if hasattr(sys.stdout, 'buffer'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

OLLAMA_URL = "http://localhost:11434"
MODEL = "qwen2.5:7b"

# ── Промты ────────────────────────────────────────────────────────────────────

REWRITE_PROMPT = """Ты — помощник юриста. Переформулируй запрос пользователя в юридические КЛЮЧЕВЫЕ СЛОВА для поиска по базе нормативных правовых актов России.

Правила:
- Верни ТОЛЬКО 5-10 ключевых юридических слов через пробел, без вопросов и предложений
- Используй существительные в именительном/родительном падеже
- Используй термины из российского законодательства (с указанием кодекса/статьи если очевидно)
- Не пиши вопросы, союзы, пояснения

Примеры:
Запрос: "сосед затопил квартиру кто платит"
Ключевые слова: возмещение ущерба залитие жилого помещения ответственность собственника ГК РФ статья 1064

Запрос: "работодатель не платит зарплату 3 месяца"
Ключевые слова: задержка выплаты заработной платы материальная ответственность работодателя статья 236 ТК РФ

Запрос: "врач допустил ошибку при операции"
Ключевые слова: врачебная ошибка медицинская халатность ответственность медицинской организации возмещение вреда здоровью

Запрос: "соседи шумят ночью что делать"
Ключевые слова: нарушение тишины ночное время КоАП РФ статья 6.3 административная ответственность шум жилой дом

Запрос: "партнёр вывел деньги из общего бизнеса"
Ключевые слова: вывод активов участником ООО ущерб обществу субсидиарная ответственность статья 53.1 ГК РФ

Запрос: {query}
Ключевые слова:"""

ANSWER_PROMPT = """Ты — юридический ассистент. Отвечай строго на основе предоставленных документов. Если ответа в документах нет — скажи об этом прямо.

Правила ответа:
- Отвечай по существу, 3-5 предложений
- Ссылайся на документы в формате [1], [2] и т.д.
- Указывай конкретные статьи и нормы если они есть в документах
- Не придумывай информацию которой нет в документах
- Пиши на русском языке

Документы:
{documents}

Вопрос: {query}

Ответ:"""

# ── Ollama API ─────────────────────────────────────────────────────────────────

def ollama_generate(prompt: str, stream: bool = True) -> str:
    payload = json.dumps({
        "model": MODEL,
        "prompt": prompt,
        "stream": stream,
        "options": {
            "temperature": 0.1,   # низкая температура — точность важнее творчества
            "num_predict": 512,
        }
    }).encode()

    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
    )

    result = []
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            if stream:
                for line in resp:
                    chunk = json.loads(line.decode())
                    token = chunk.get("response", "")
                    print(token, end="", flush=True)
                    result.append(token)
                    if chunk.get("done"):
                        break
                print()
            else:
                data = json.loads(resp.read())
                result.append(data.get("response", ""))
    except urllib.error.URLError as e:
        print(f"\nОШИБКА: не удалось подключиться к Ollama ({OLLAMA_URL})")
        print(f"  {e}")
        print("Убедитесь что Ollama запущена. Запустите: ollama serve")
        sys.exit(1)

    return "".join(result).strip()


def check_ollama():
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=5) as resp:
            data = json.loads(resp.read())
            models = [m["name"] for m in data.get("models", [])]
            if not any(MODEL in m for m in models):
                print(f"ОШИБКА: модель '{MODEL}' не найдена в Ollama.")
                print(f"Скачайте: ollama pull {MODEL}")
                sys.exit(1)
            print(f"Ollama OK  |  модель '{MODEL}' доступна\n")
    except urllib.error.URLError:
        print(f"ОШИБКА: Ollama не запущена по адресу {OLLAMA_URL}")
        print("Запустите: ollama serve")
        sys.exit(1)


# ── Шаги pipeline ──────────────────────────────────────────────────────────────

def rewrite_query(query: str) -> str:
    print(f"[Query rewriting] '{query}'")
    print("→ ", end="", flush=True)
    t0 = time.time()
    result = ollama_generate(REWRITE_PROMPT.format(query=query), stream=True)
    print(f"  ({time.time()-t0:.1f}с)\n")
    return result


def generate_answer(query: str, chunks: list[dict]) -> str:
    docs_text = ""
    for i, chunk in enumerate(chunks, 1):
        title = chunk.get("title", "Без названия")
        date = chunk.get("date", "")
        text = chunk.get("text", "")[:600]
        docs_text += f"[{i}] {title}"
        if date:
            docs_text += f" ({date})"
        docs_text += f"\n{text}\n\n"

    prompt = ANSWER_PROMPT.format(documents=docs_text.strip(), query=query)

    print("[Генерация ответа]")
    t0 = time.time()
    result = ollama_generate(prompt, stream=True)
    print(f"\n  ({time.time()-t0:.1f}с)\n")
    return result


# ── Демо-данные ────────────────────────────────────────────────────────────────

DEMO_CASES = [
    {
        "query": "сосед затопил квартиру кто платит",
        "chunks": [
            {
                "title": "Гражданский кодекс Российской Федерации. Часть вторая",
                "date": "26.01.1996",
                "text": "Статья 1064. Общие основания ответственности за причинение вреда. "
                        "Вред, причиненный личности или имуществу гражданина, а также вред, "
                        "причиненный имуществу юридического лица, подлежит возмещению в полном "
                        "объеме лицом, причинившим вред. Законом обязанность возмещения вреда "
                        "может быть возложена на лицо, не являющееся причинителем вреда.",
            },
            {
                "title": "Жилищный кодекс Российской Федерации",
                "date": "29.12.2004",
                "text": "Статья 30. Права и обязанности собственника жилого помещения. "
                        "Собственник жилого помещения обязан поддерживать данное помещение "
                        "в надлежащем состоянии, не допуская бесхозяйственного обращения с ним, "
                        "соблюдать права и законные интересы соседей, правила пользования "
                        "жилыми помещениями, а также правила содержания общего имущества.",
            },
        ],
    },
    {
        "query": "работодатель не платит зарплату 3 месяца",
        "chunks": [
            {
                "title": "Трудовой кодекс Российской Федерации",
                "date": "30.12.2001",
                "text": "Статья 236. Материальная ответственность работодателя за задержку "
                        "выплаты заработной платы. При нарушении работодателем установленного "
                        "срока выплаты заработной платы работодатель обязан выплатить её "
                        "с уплатой процентов в размере не ниже одной сто пятидесятой "
                        "действующей ключевой ставки Банка России за каждый день задержки.",
            },
            {
                "title": "О внесении изменений в отдельные законодательные акты по вопросам "
                         "повышения ответственности работодателей за нарушения оплаты труда",
                "date": "03.07.2016",
                "text": "При нарушении работодателем установленного срока выплаты заработной "
                        "платы, оплаты отпуска, выплат при увольнении работодатель обязан "
                        "выплатить их с уплатой процентов. Работник вправе обратиться в суд "
                        "за защитой своих прав в течение одного года со дня установленного "
                        "срока выплаты.",
            },
        ],
    },
    {
        "query": "рецепт борща",
        "chunks": [],   # намеренно пустые чанки — тест на out-of-domain
    },
]


# ── Full pipeline (embed → retrieve → rerank → LLM) ──────────────────────────

def run_full_pipeline(query: str):
    # Импортируем компоненты pipeline
    sys.path.insert(0, "tests")
    try:
        from test_pipeline import load_models, search, QDRANT_URL, COLLECTION
        from qdrant_client import QdrantClient
    except ImportError as e:
        print(f"ОШИБКА импорта pipeline: {e}")
        sys.exit(1)

    client = QdrantClient(url=QDRANT_URL, timeout=10)
    embedder, reranker = load_models()

    print(f"\n{'='*70}")
    print(f"Запрос: {query}")
    print(f"{'='*70}\n")

    # 1. Query rewriting
    rewritten = rewrite_query(query)

    # 2. Retrieve + rerank
    print(f"[Поиск] по запросу: '{rewritten}'")
    results = search(rewritten, embedder, reranker, client)

    if not results:
        print("Ничего не найдено — запрос вне домена или нет релевантных документов.\n")
        return

    chunks = [
        {
            "title": hit.payload.get("title", ""),
            "date": hit.payload.get("date", ""),
            "text": hit.payload.get("text", ""),
            "score": float(score),
        }
        for score, hit in results
    ]

    print(f"Найдено {len(chunks)} документов:\n")
    for i, c in enumerate(chunks, 1):
        print(f"  [{i}] {c['title'][:70]} (score={c['score']:.2f})")
    print()

    # 3. Generate answer
    generate_answer(rewritten, chunks)


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    global OLLAMA_URL
    parser = argparse.ArgumentParser(description="Тест LLM-генерации ответов")
    parser.add_argument("query", nargs="?", help="Запрос для полного pipeline (с --full)")
    parser.add_argument("--demo", action="store_true", help="Запустить демо-сценарии")
    parser.add_argument("--full", action="store_true", help="Полный pipeline: embed→retrieve→rerank→LLM")
    parser.add_argument("--no-rewrite", action="store_true", help="Пропустить query rewriting")
    parser.add_argument("--ollama", default=None, help=f"URL Ollama (по умолчанию {OLLAMA_URL})")
    args = parser.parse_args()

    if args.ollama:
        OLLAMA_URL = args.ollama

    check_ollama()

    if args.full:
        if not args.query:
            print("Укажите запрос: python tests/test_llm.py --full 'ваш запрос'")
            sys.exit(1)
        run_full_pipeline(args.query)
        return

    cases = DEMO_CASES if args.demo else None

    if cases is None:
        # Интерактивный режим
        print("Введите запрос (или 'exit' для выхода):")
        while True:
            query = input("\n> ").strip()
            if query.lower() in ("exit", "quit", "q"):
                break
            if not query:
                continue

            print()
            rewritten = query if args.no_rewrite else rewrite_query(query)
            print("Введите чанки в формате JSON (или Enter чтобы пропустить):")
            print('Пример: [{"title": "...", "date": "...", "text": "..."}]')
            raw = input("> ").strip()
            chunks = json.loads(raw) if raw else []
            print()
            generate_answer(rewritten, chunks)
        return

    # Демо-режим
    for case in cases:
        query = case["query"]
        chunks = case["chunks"]

        print(f"\n{'='*70}")
        print(f"Запрос: {query}")
        print(f"{'='*70}\n")

        if not chunks:
            print("[Нет чанков — тест out-of-domain]\n")
            rewritten = rewrite_query(query)
            generate_answer(rewritten, [])
            continue

        rewritten = query if args.no_rewrite else rewrite_query(query)
        generate_answer(rewritten, chunks)


if __name__ == "__main__":
    main()
