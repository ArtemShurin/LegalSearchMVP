from pydantic import BaseModel
from fastapi import APIRouter, Depends

from src.models.user import User
from src.services import activity_log
from src.routers.auth import get_current_user_optional

router = APIRouter(prefix="/telemetry", tags=["telemetry"])

# Белый список допустимых event_type с фронтенда — защита от того, что
# скомпрометированный/кастомный клиент начнёт писать произвольные типы
# событий или заваливать лог мусором под видом служебных (api_request,
# search_pipeline — те пишет только сам backend).
# search_request_sent/search_sources_shown/search_answer_rendered —
# моменты жизненного цикла поиска, зафиксированные на фронте В МОМЕНТ
# самого события (client_ts в payload, см. frontend/src/lib/telemetry.ts),
# а не когда они доехали до backend — явный запрос пользователя 2026-11-04.
_ALLOWED_EVENT_TYPES = {
    "page_view", "click", "input_change",
    "search_request_sent", "search_sources_shown", "search_answer_rendered",
}


class TelemetryEvent(BaseModel):
    event_type: str
    session_id: str
    payload: dict = {}


class TelemetryBatch(BaseModel):
    events: list[TelemetryEvent]


@router.post("/events")
async def post_events(
    batch: TelemetryBatch,
    current_user: User | None = Depends(get_current_user_optional),
):
    """
    Пачка клиентских событий (клики/переходы/ввод) с фронтенда — см.
    frontend/src/lib/telemetry.ts. current_user опционален: события со
    страниц /login и /register происходят ДО авторизации, это ожидаемо,
    не ошибка. session_id — единственная связка таких событий в одну цепочку
    до момента логина.
    """
    for event in batch.events:
        if event.event_type not in _ALLOWED_EVENT_TYPES:
            continue
        await activity_log.log_event(
            event.event_type,
            user_id=str(current_user.id) if current_user else None,
            company_id=str(current_user.company_id) if current_user and current_user.company_id else None,
            session_id=event.session_id,
            payload=event.payload,
        )
    return {"status": "ok"}
