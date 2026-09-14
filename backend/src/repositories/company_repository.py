from sqlalchemy.ext.asyncio import AsyncSession

from src.models.company import Company


class CompanyRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def create(self, name: str) -> Company:
        company = Company(name=name)
        self.db.add(company)
        await self.db.flush()
        return company
