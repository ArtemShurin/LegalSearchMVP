FROM python:3.11-slim

WORKDIR /app

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ .
# ml/ — отдельный пакет (embedding/reranker/LLM), живёт вне backend/, см.
# legalsearch/ml/. Копируется как сосед src/ внутри /app, чтобы `import ml`
# работал так же, как сейчас работает `import src` (сборка теперь идёт из
# корня репозитория — context: . в docker-compose.yml, не ./backend).
COPY ml/ ./ml/

CMD alembic upgrade head && uvicorn src.main:app --host 0.0.0.0 --port 8000 --reload
