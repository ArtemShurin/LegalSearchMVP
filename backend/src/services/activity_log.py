"""
Запись действий пользователя (клики, ввод, навигация, HTTP-запросы,
диагностика поискового пайплайна) в activity_events. См. src/models/activity.py.

Каждый вызов открывает СВОЮ сессию (не переиспользует db-сессию запроса) —
лог не должен зависеть от commit/rollback основной бизнес-транзакции и
наоборот: упавший поиск всё равно должен оставить след в логе.
"""
from datetime import datetime

from jose import JWTError

from src.config import settings
from src.database import AsyncSessionLocal
from src.models.activity import ActivityEvent
from src.services.auth import decode_token

# Ключи payload, под которыми может оказаться СЫРОЙ текст, введённый
# пользователем (поисковый запрос, значения полей форм) — именно их трогает
# settings.log_input_content. Всё остальное в payload (score, timing,
# document_id, event_type деталей UI-элемента и т.п.) — не текст, который
# пользователь печатал, редактировать не нужно.
_RAW_CONTENT_KEYS = {"query", "rewritten_query", "value", "text"}

# Эти ключи не логируются НИКОГДА, независимо от settings.log_input_content —
# защита от случайной утечки, если кто-то по ошибке передаст пароль в payload.
_NEVER_LOG_KEYS = {"password", "password_hash", "hashed_password", "access_token", "token"}


def _redact(payload: dict) -> dict:
    result = {}
    for key, value in payload.items():
        if key in _NEVER_LOG_KEYS:
            continue
        if not settings.log_input_content and key in _RAW_CONTENT_KEYS and isinstance(value, str):
            result[f"{key}_len"] = len(value)
            continue
        if isinstance(value, dict):
            result[key] = _redact(value)
        elif isinstance(value, list):
            result[key] = [_redact(v) if isinstance(v, dict) else v for v in value]
        else:
            result[key] = value
    return result


async def log_event(
    event_type: str,
    *,
    user_id: str | None = None,
    company_id: str | None = None,
    session_id: str | None = None,
    payload: dict | None = None,
) -> None:
    try:
        async with AsyncSessionLocal() as session:
            session.add(ActivityEvent(
                user_id=user_id,
                company_id=company_id,
                session_id=session_id,
                event_type=event_type,
                payload=_redact(payload) if payload else None,
                created_at=datetime.utcnow(),
            ))
            await session.commit()
    except Exception:
        # Лог — вспомогательная функция, падение записи лога не должно ронять
        # сам запрос пользователя (поиск/клик/etc).
        pass


def user_id_from_auth_header(authorization: str | None) -> str | None:
    """
    Достаёт user_id из заголовка Authorization без похода в БД (только
    декодирование JWT) — для лёгкого access-log middleware, которому нельзя
    позволить замедлять КАЖДЫЙ запрос лишним запросом к Postgres. Молча
    возвращает None на любой проблеме (протух токен, мусор в заголовке и
    т.п.) — это вспомогательная функция для лога, а не авторизация.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    try:
        return decode_token(authorization[7:])
    except JWTError:
        return None
