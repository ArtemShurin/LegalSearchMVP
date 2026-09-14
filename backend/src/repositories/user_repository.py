import uuid
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from src.models.user import User


class UserRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_by_id(self, user_id: uuid.UUID) -> User | None:
        result = await self.db.execute(select(User).where(User.id == user_id))
        return result.scalar_one_or_none()

    async def get_by_email(self, email: str) -> User | None:
        result = await self.db.execute(select(User).where(User.email == email))
        return result.scalar_one_or_none()

    async def create(self, email: str, password_hash: str, company_id: uuid.UUID) -> User:
        user = User(email=email, password_hash=password_hash, company_id=company_id)
        self.db.add(user)
        await self.db.flush()
        return user
