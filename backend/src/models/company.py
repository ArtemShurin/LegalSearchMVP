import uuid
from datetime import datetime

from sqlalchemy import String, DateTime
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID

from src.database import Base


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    subscription: Mapped[str] = mapped_column(String(50), default="trial")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    users = relationship("User", back_populates="company")
    documents = relationship("Document", back_populates="company", cascade="all, delete-orphan")

