"""
Общая инфраструктура для интеграционных тестов backend.

Тесты бьют по РЕАЛЬНО ЗАПУЩЕННОМУ серверу (docker compose, localhost:8000)
и реальному Postgres — это не юнит-тесты с моками, а проверка "не падает ли бэк".

Все тестовые пользователи создаются с email вида *@backendtest.io —
это позволяет отличить тестовые данные от настоящих и почистить их после прогона.
"""
import time
import uuid

import jwt
import pytest
import requests

BASE_URL = "http://localhost:8000"
SECRET_KEY = "super-secret-key-change-in-production"  # из backend/.env, только для тестов
ALGORITHM = "HS256"
TEST_EMAIL_DOMAIN = "backendtest.io"


def unique_email() -> str:
    return f"test-{uuid.uuid4().hex[:12]}@{TEST_EMAIL_DOMAIN}"


def register_user(email: str | None = None, password: str = "TestPass123!", company_name: str = "Test Company") -> dict:
    """Регистрирует нового пользователя, возвращает {email, password, token, company_name}."""
    email = email or unique_email()
    resp = requests.post(f"{BASE_URL}/auth/register", json={
        "email": email,
        "password": password,
        "company_name": company_name,
    })
    resp.raise_for_status()
    token = resp.json()["access_token"]
    return {"email": email, "password": password, "token": token, "company_name": company_name}


def auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def make_token(sub: str, secret: str = SECRET_KEY, algorithm: str = ALGORITHM, expires_in: int = 3600) -> str:
    payload = {"sub": sub, "exp": int(time.time()) + expires_in}
    return jwt.encode(payload, secret, algorithm=algorithm)


def make_expired_token(sub: str) -> str:
    payload = {"sub": sub, "exp": int(time.time()) - 3600}
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


@pytest.fixture(scope="session", autouse=True)
def check_server_up():
    try:
        r = requests.get(f"{BASE_URL}/", timeout=5)
        assert r.status_code == 200
    except Exception as e:
        pytest.exit(f"Backend недоступен на {BASE_URL}: {e}")
    yield


@pytest.fixture
def user_a():
    return register_user()


@pytest.fixture
def user_b():
    return register_user()
