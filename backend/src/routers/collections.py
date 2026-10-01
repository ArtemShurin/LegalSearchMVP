from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.models.user import User
from src.schemas.collection import CollectionCreate, CollectionItemAdd, CollectionRename, CollectionResponse
from src.repositories.collection_repository import CollectionRepository
from src.routers.auth import get_current_user

router = APIRouter(prefix="/collections", tags=["collections"])


@router.get("/", response_model=list[CollectionResponse])
async def get_collections(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await CollectionRepository(db).get_by_user(current_user.id)


@router.post("/", response_model=CollectionResponse)
async def create_collection(
    data: CollectionCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    repo = CollectionRepository(db)
    collection = await repo.create(current_user.id, data.name)
    await db.commit()
    await db.refresh(collection, attribute_names=["items"])
    return collection


@router.patch("/{collection_id}", response_model=CollectionResponse)
async def rename_collection(
    collection_id: UUID,
    data: CollectionRename,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    repo = CollectionRepository(db)
    collection = await repo.get_by_id(collection_id, current_user.id)
    if not collection:
        raise HTTPException(status_code=404, detail="Подборка не найдена")

    await repo.rename(collection, data.name)
    await db.commit()
    await db.refresh(collection, attribute_names=["items"])
    return collection


@router.post("/{collection_id}/items")
async def add_item(
    collection_id: UUID,
    data: CollectionItemAdd,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    repo = CollectionRepository(db)
    collection = await repo.get_by_id(collection_id, current_user.id)
    if not collection:
        raise HTTPException(status_code=404, detail="Подборка не найдена")

    if await repo.item_exists(collection.id, data.document_id):
        raise HTTPException(status_code=409, detail="Документ уже добавлен в эту подборку")

    await repo.add_item(collection.id, data.document_id, data.title, data.fragment, data.relevance)
    await db.commit()
    return {"status": "ok"}


@router.delete("/{collection_id}")
async def delete_collection(
    collection_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    repo = CollectionRepository(db)
    collection = await repo.get_by_id(collection_id, current_user.id)
    if not collection:
        raise HTTPException(status_code=404, detail="Подборка не найдена")

    await repo.delete(collection)
    await db.commit()
    return {"status": "ok"}


@router.delete("/{collection_id}/items/{item_id}")
async def delete_item(
    collection_id: UUID,
    item_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    repo = CollectionRepository(db)
    collection = await repo.get_by_id(collection_id, current_user.id)
    if not collection:
        raise HTTPException(status_code=404, detail="Подборка не найдена")

    item = await repo.get_item(item_id, collection.id)
    if not item:
        raise HTTPException(status_code=404, detail="Элемент не найден")

    await repo.delete_item(item)
    await db.commit()
    return {"status": "ok"}
