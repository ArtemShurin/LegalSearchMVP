from jose import JWTError
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.models.user import User
from src.schemas.auth import RegisterRequest, LoginRequest, TokenResponse, UserResponse
from src.services.auth import hash_password, verify_password, create_access_token, decode_token
from src.repositories.user_repository import UserRepository
from src.repositories.company_repository import CompanyRepository

router = APIRouter(prefix="/auth", tags=["auth"])
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


@router.post("/register", response_model=TokenResponse)
async def register(data: RegisterRequest, db: AsyncSession = Depends(get_db)):
    user_repo = UserRepository(db)
    company_repo = CompanyRepository(db)

    if await user_repo.get_by_email(data.email):
        raise HTTPException(status_code=400, detail="Email уже зарегистрирован")

    try:
        company = await company_repo.create(data.company_name)
        user = await user_repo.create(data.email, hash_password(data.password), company.id)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=400, detail="Email уже зарегистрирован")

    return TokenResponse(access_token=create_access_token(str(user.id)))


@router.post("/login", response_model=TokenResponse)
async def login(data: LoginRequest, db: AsyncSession = Depends(get_db)):
    user_repo = UserRepository(db)
    user = await user_repo.get_by_email(data.email)

    if not user or not verify_password(data.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Неверный email или пароль")

    return TokenResponse(access_token=create_access_token(str(user.id)))


@router.get("/me", response_model=UserResponse)
async def me(token: str = Depends(oauth2_scheme), db: AsyncSession = Depends(get_db)):
    user = await _get_user_from_token(token, db)
    return user


async def get_current_user(token: str = Depends(oauth2_scheme), db: AsyncSession = Depends(get_db)) -> User:
    return await _get_user_from_token(token, db)


async def _get_user_from_token(token: str, db: AsyncSession) -> User:
    try:
        user_id = decode_token(token)
    except JWTError:
        raise HTTPException(status_code=401, detail="Не авторизован")

    try:
        user = await UserRepository(db).get_by_id(user_id)
    except (ValueError, DBAPIError):
        raise HTTPException(status_code=401, detail="Не авторизован")

    if not user:
        raise HTTPException(status_code=401, detail="Не авторизован")
    return user
