# Backend integration tests

Бьют по реально запущенному API (`docker compose up`, `http://localhost:8000`)
и реальному Postgres. Не мокают ничего — проверяют, что бэк не падает.

AI-часть (`/search/`) подключена к реальному пайплайну (`search_pipeline.py`:
embed → retrieve → rerank → LLM rewriting при необходимости → LLM-ответ), поэтому
тесты также требуют поднятую Ollama (`qwen2.5:7b`) на хосте — иначе LLM-шаги
тихо деградируют в fallback (см. `llm.LLMUnavailableError`), но эндпоинт всё
равно отвечает 200. Тесты проверяют форму ответа и то, что бэк не падает,
а не качество результатов поиска — за это отвечают `scripts/test_*.py`.

## Запуск

```
docker compose up -d          # backend + postgres + qdrant должны быть подняты
ollama serve                  # Ollama на хосте, модель qwen2.5:7b должна быть pull'нута
cd backend/tests
pip install -r requirements.txt
pytest -v
```

Тестовые пользователи создаются с email `*@backendtest.io` — их можно
найти в БД и почистить командой:

```sql
DELETE FROM users WHERE email LIKE '%@backendtest.io';
DELETE FROM companies WHERE name IN ('Test Company');  -- осторожно, могут быть не только тестовые
```
