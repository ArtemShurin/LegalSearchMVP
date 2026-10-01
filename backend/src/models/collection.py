import uuid
from datetime import datetime

from sqlalchemy import String, DateTime, ForeignKey, Text, Float
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID

from src.database import Base


class Collection(Base):
    __tablename__ = "collections"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="collections")
    items = relationship("CollectionItem", back_populates="collection", cascade="all, delete-orphan")


class CollectionItem(Base):
    __tablename__ = "collection_items"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    collection_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("collections.id"), nullable=False)
    document_id: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=True)
    fragment: Mapped[str] = mapped_column(Text, nullable=True)
    relevance: Mapped[float] = mapped_column(Float, nullable=True)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    collection = relationship("Collection", back_populates="items")

