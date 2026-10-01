from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.models.user import User
from src.repositories.query_repository import QueryRepository
from src.routers.auth import get_current_user

router = APIRouter(prefix="/history", tags=["history"])


@router.get("/")
async def get_history(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    queries = await QueryRepository(db).get_user_history(current_user.id)
    return [
        {
            "id": str(q.id),
            "text": q.text,
            "answer": q.answer,
            "created_at": q.created_at,
            "sources": [
                {"document_id": s.document_id, "title": s.title, "relevance": s.relevance, "fragment": s.fragment}
                for s in q.sources
            ],
        }
        for q in queries
    ]
