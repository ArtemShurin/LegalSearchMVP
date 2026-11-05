from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str
    secret_key: str
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 60
    qdrant_url: str = "http://localhost:6333"
    redis_url: str = "redis://localhost:6379"
    ollama_url: str = "http://localhost:11434"
    llm_model: str = "qwen2.5:7b"
    uploads_dir: str = "/app/uploads"
    upload_max_file_size: int = 20 * 1024 * 1024       # 20 МБ на файл
    upload_company_quota: int = 15 * 1024 * 1024 * 1024  # 15 ГБ на компанию
    # Rate limit на /search/ по компании (или пользователю, если компании нет) —
    # без него один активный клиент забивает очередь LLM для всех остальных,
    # особенно критично при единственной GPU (см. memory:
    # project-backend-production-readiness, пункт 4). Отдельно от nginx'ового
    # rate limiting по IP (пункт 3) — тот не знает про company_id/auth.
    rate_limit_search_per_minute: int = 10
    # Пусто по умолчанию — Sentry не включается без реального DSN (аккаунт
    # нужно завести отдельно, не часть кода). См. memory:
    # project-backend-production-readiness, пункт 5.
    sentry_dsn: str = ""
    # Логирование действий пользователей (клики/ввод/навигация/поисковый
    # пайплайн) — см. src/services/activity_log.py. True — на этапе тестов,
    # логируем ВСЁ, включая сырой текст запросов и введённых значений форм
    # (кроме пароля — тот не логируется никогда, независимо от этого флага).
    # Явный запрос пользователя 2026-11-04: позже, когда войдём в прод с
    # реальными клиентами юрфирм, нужно будет выключить (False) — тогда
    # сырой текст в payload заменяется на длину, сам факт события остаётся.
    log_input_content: bool = True

    class Config:
        env_file = ".env"


settings = Settings()

