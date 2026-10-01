import uuid
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from src.models.collection import Collection, CollectionItem


class CollectionRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_by_user(self, user_id: uuid.UUID) -> list[Collection]:
        result = await self.db.execute(
            select(Collection)
            .where(Collection.user_id == user_id)
            .options(selectinload(Collection.items))
        )
        return result.scalars().all()

    async def get_by_id(self, collection_id: uuid.UUID, user_id: uuid.UUID) -> Collection | None:
        result = await self.db.execute(
            select(Collection).where(
                Collection.id == collection_id,
                Collection.user_id == user_id,
            )
        )
        return result.scalar_one_or_none()

    async def create(self, user_id: uuid.UUID, name: str) -> Collection:
        collection = Collection(user_id=user_id, name=name)
        self.db.add(collection)
        await self.db.flush()
        return collection

    async def item_exists(self, collection_id: uuid.UUID, document_id: str) -> bool:
        result = await self.db.execute(
            select(CollectionItem).where(
                CollectionItem.collection_id == collection_id,
                CollectionItem.document_id == document_id,
            )
        )
        return result.scalar_one_or_none() is not None

    async def add_item(
        self,
        collection_id: uuid.UUID,
        document_id: str,
        title: str,
        fragment: str | None = None,
        relevance: float | None = None,
    ) -> CollectionItem:
        item = CollectionItem(
            collection_id=collection_id,
            document_id=document_id,
            title=title,
            fragment=fragment,
            relevance=relevance,
        )
        self.db.add(item)
        await self.db.flush()
        return item

    async def delete(self, collection: Collection) -> None:
        await self.db.delete(collection)

    async def rename(self, collection: Collection, name: str) -> Collection:
        collection.name = name
        await self.db.flush()
        return collection

    async def get_item(self, item_id: uuid.UUID, collection_id: uuid.UUID) -> CollectionItem | None:
        result = await self.db.execute(
            select(CollectionItem).where(
                CollectionItem.id == item_id,
                CollectionItem.collection_id == collection_id,
            )
        )
        return result.scalar_one_or_none()

    async def delete_item(self, item: CollectionItem) -> None:
        await self.db.delete(item)
