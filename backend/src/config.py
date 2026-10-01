from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str
    secret_key: str
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 60
    qdrant_url: str = "http://localhost:6333"
    ollama_url: str = "http://localhost:11434"
    llm_model: str = "qwen2.5:7b"
    uploads_dir: str = "/app/uploads"
    upload_max_file_size: int = 20 * 1024 * 1024       # 20 МБ на файл
    upload_company_quota: int = 15 * 1024 * 1024 * 1024  # 15 ГБ на компанию

    class Config:
        env_file = ".env"


settings = Settings()

