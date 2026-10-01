import uuid
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from src.models.document import Document


class DocumentRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def create(
        self,
        company_id: uuid.UUID,
        uploaded_by: uuid.UUID,
        filename: str,
        file_path: str,
        file_size: int,
    ) -> Document:
        doc = Document(
            company_id=company_id,
            uploaded_by=uploaded_by,
            filename=filename,
            file_path=file_path,
            file_size=file_size,
        )
        self.db.add(doc)
        await self.db.flush()
        return doc

    async def get_by_company(self, company_id: uuid.UUID) -> list[Document]:
        result = await self.db.execute(
            select(Document).where(Document.company_id == company_id).order_by(Document.created_at.desc())
        )
        return result.scalars().all()

    async def get_by_id(self, doc_id: uuid.UUID, company_id: uuid.UUID) -> Document | None:
        result = await self.db.execute(
            select(Document).where(Document.id == doc_id, Document.company_id == company_id)
        )
        return result.scalar_one_or_none()

    async def total_size_for_company(self, company_id: uuid.UUID) -> int:
        result = await self.db.execute(
            select(func.coalesce(func.sum(Document.file_size), 0)).where(Document.company_id == company_id)
        )
        return result.scalar_one()

    async def set_status(self, doc_id: uuid.UUID, status: str, error_message: str | None = None) -> None:
        result = await self.db.execute(select(Document).where(Document.id == doc_id))
        doc = result.scalar_one_or_none()
        if doc:
            doc.status = status
            doc.error_message = error_message

    async def delete(self, doc: Document) -> None:
        await self.db.delete(doc)
