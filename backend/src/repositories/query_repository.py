import uuid
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from src.models.query import Query, QuerySource


class QueryRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def create(self, user_id: uuid.UUID, text: str, answer: str, filters: dict | None) -> Query:
        query = Query(user_id=user_id, text=text, answer=answer, filters=filters)
        self.db.add(query)
        await self.db.flush()
        return query

    async def add_sources(self, query_id: uuid.UUID, sources: list[dict]) -> None:
        for s in sources:
            self.db.add(QuerySource(
                query_id=query_id,
                document_id=s["document_id"],
                title=s["title"],
                relevance=s["relevance"],
                fragment=s["fragment"],
            ))

    async def get_user_history(self, user_id: uuid.UUID, limit: int = 50) -> list[Query]:
        result = await self.db.execute(
            select(Query)
            .where(Query.user_id == user_id)
            .options(selectinload(Query.sources))
            .order_by(Query.created_at.desc())
            .limit(limit)
        )
        return result.scalars().all()
