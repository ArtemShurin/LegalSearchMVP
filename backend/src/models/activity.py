import uuid
from datetime import datetime

from sqlalchemy import String, DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.postgresql import UUID, JSONB

from src.database import Base


class ActivityEvent(Base):
    """
    Полный журнал действий пользователя на сайте — клики, ввод, переходы между
    страницами, HTTP-запросы к backend, диагностика поискового пайплайна.
    Независим от основных бизнес-таблиц (queries/documents/...) — пишется из
    src.services.activity_log.log_event(), своей собственной сессией, чтобы
    падение/роллбек основной транзакции не терял записи лога и наоборот.

    session_id — генерируется на фронте (localStorage), живёт ДО логина тоже
    (события на /login и /register ещё без user_id) — единственный способ
    связать весь путь одного человека по сайту в одну цепочку.

    payload — структура разная у каждого event_type (page_view/click/
    input_change/api_request/search_pipeline/...), поэтому JSONB, а не
    отдельные колонки. Сырой текст внутри payload (запросы, введённые
    значения) редактируется в log_event() в зависимости от
    settings.log_input_content — см. там.
    """
    __tablename__ = "activity_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    company_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("companies.id"), nullable=True)
    session_id: Mapped[str] = mapped_column(String(64), nullable=True)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
