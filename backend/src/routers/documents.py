import asyncio
import os
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, UploadFile, File
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.database import AsyncSessionLocal, get_db
from src.models.user import User
from src.schemas.document import DocumentResponse
from src.repositories.document_repository import DocumentRepository
from src.routers.auth import get_current_user
from src.services.document_indexer import index_document_sync, delete_document_points

router = APIRouter(prefix="/documents", tags=["documents"])


@router.get("/", response_model=list[DocumentResponse])
async def list_documents(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await DocumentRepository(db).get_by_company(current_user.company_id)


@router.post("/", response_model=DocumentResponse)
async def upload_document(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=415, detail="Принимаются только PDF-файлы")

    contents = await file.read()
    size = len(contents)

    if size == 0:
        raise HTTPException(status_code=400, detail="Пустой файл")
    if size > settings.upload_max_file_size:
        limit_mb = settings.upload_max_file_size // (1024 * 1024)
        raise HTTPException(status_code=413, detail=f"Файл больше {limit_mb} МБ")

    repo = DocumentRepository(db)
    used = await repo.total_size_for_company(current_user.company_id)
    if used + size > settings.upload_company_quota:
        quota_gb = settings.upload_company_quota / (1024 ** 3)
        raise HTTPException(status_code=413, detail=f"Превышена квота компании ({quota_gb:.0f} ГБ)")

    doc = await repo.create(
        company_id=current_user.company_id,
        uploaded_by=current_user.id,
        filename=file.filename,
        file_path="",
        file_size=size,
    )

    os.makedirs(settings.uploads_dir, exist_ok=True)
    file_path = os.path.join(settings.uploads_dir, f"{doc.id}.pdf")
    with open(file_path, "wb") as f:
        f.write(contents)
    doc.file_path = file_path

    await db.commit()
    await db.refresh(doc)

    background_tasks.add_task(_index_document_task, doc.id, current_user.company_id, file.filename, file_path)

    return doc


@router.delete("/{document_id}")
async def delete_document(
    document_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    repo = DocumentRepository(db)
    doc = await repo.get_by_id(document_id, current_user.company_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Документ не найден")

    await asyncio.to_thread(delete_document_points, str(doc.id))

    if doc.file_path and os.path.exists(doc.file_path):
        os.remove(doc.file_path)

    await repo.delete(doc)
    await db.commit()
    return {"status": "ok"}


async def _index_document_task(doc_id: uuid.UUID, company_id: uuid.UUID, filename: str, file_path: str) -> None:
    """Фоновая индексация — выполняется ПОСЛЕ отправки ответа клиенту, со своей
    отдельной сессией БД (сессия из запроса к этому моменту уже закрыта)."""
    async with AsyncSessionLocal() as db:
        repo = DocumentRepository(db)
        try:
            with open(file_path, "rb") as f:
                pdf_bytes = f.read()
            await asyncio.to_thread(index_document_sync, str(doc_id), str(company_id), filename, pdf_bytes)
            await repo.set_status(doc_id, "indexed")
        except Exception as e:
            await repo.set_status(doc_id, "failed", str(e)[:2000])
        finally:
            await db.commit()
