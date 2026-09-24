import uuid
from concurrent.futures import ThreadPoolExecutor

import requests

from conftest import BASE_URL, auth_headers, make_expired_token, make_token, register_user, unique_email


# ── Регистрация ──────────────────────────────────────────────────────────

def test_register_success():
    email = unique_email()
    r = requests.post(f"{BASE_URL}/auth/register", json={
        "email": email, "password": "TestPass123!", "company_name": "ООО Ромашка",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert "access_token" in body
    assert body["token_type"] == "bearer"


def test_register_duplicate_email():
    email = unique_email()
    r1 = requests.post(f"{BASE_URL}/auth/register", json={
        "email": email, "password": "pass1", "company_name": "Company 1",
    })
    assert r1.status_code == 200

    r2 = requests.post(f"{BASE_URL}/auth/register", json={
        "email": email, "password": "pass2", "company_name": "Company 2",
    })
    assert r2.status_code == 400, r2.text


def test_register_invalid_email_format():
    r = requests.post(f"{BASE_URL}/auth/register", json={
        "email": "not-an-email", "password": "pass", "company_name": "X",
    })
    assert r.status_code == 422, r.text


def test_register_missing_fields():
    r = requests.post(f"{BASE_URL}/auth/register", json={"email": unique_email()})
    assert r.status_code == 422


def test_register_empty_password():
    # Пустой пароль формально проходит валидацию pydantic (это просто str) —
    # проверяем, что бэк не падает 500, а либо принимает, либо валидирует.
    r = requests.post(f"{BASE_URL}/auth/register", json={
        "email": unique_email(), "password": "", "company_name": "X",
    })
    assert r.status_code in (200, 400, 422), r.text


def test_register_empty_company_name():
    r = requests.post(f"{BASE_URL}/auth/register", json={
        "email": unique_email(), "password": "pass", "company_name": "",
    })
    assert r.status_code in (200, 400, 422), r.text


def test_register_very_long_password():
    # password ограничен max_length=200 в схеме — длинный пароль должен давать
    # чистую 422-валидацию, а не падать 500 внутри passlib/bcrypt
    r = requests.post(f"{BASE_URL}/auth/register", json={
        "email": unique_email(), "password": "a" * 5000, "company_name": "X",
    })
    assert r.status_code == 422, r.text


def test_register_unicode_company_name():
    r = requests.post(f"{BASE_URL}/auth/register", json={
        "email": unique_email(), "password": "pass", "company_name": "ООО «Рога и Копыта» 🏛",
    })
    assert r.status_code == 200, r.text


def test_register_sql_injection_attempt_in_company_name():
    r = requests.post(f"{BASE_URL}/auth/register", json={
        "email": unique_email(), "password": "pass",
        "company_name": "'; DROP TABLE companies; --",
    })
    assert r.status_code == 200, r.text
    # Проверяем что таблица жива — следующая регистрация должна тоже пройти
    r2 = requests.post(f"{BASE_URL}/auth/register", json={
        "email": unique_email(), "password": "pass", "company_name": "X",
    })
    assert r2.status_code == 200, "таблица companies повреждена SQL-инъекцией?"


def test_register_malformed_json():
    r = requests.post(f"{BASE_URL}/auth/register", data="not json",
                       headers={"Content-Type": "application/json"})
    assert r.status_code == 422


def test_register_wrong_content_type():
    r = requests.post(f"{BASE_URL}/auth/register",
                       data="email=a@b.com&password=x&company_name=Y")
    assert r.status_code in (400, 415, 422)


# ── Логин ─────────────────────────────────────────────────────────────────

def test_register_duplicate_email_race_condition():
    """
    Два одновременных register с одним новым email — TOCTOU-окно между
    get_by_email() и INSERT. Ожидаем: ровно один 200, остальные — чистый 400
    (unique-констрейнт), НЕ 500.
    """
    email = unique_email()

    def do_register():
        return requests.post(f"{BASE_URL}/auth/register", json={
            "email": email, "password": "pass", "company_name": "Race Co",
        })

    with ThreadPoolExecutor(max_workers=5) as pool:
        responses = list(pool.map(lambda _: do_register(), range(5)))

    statuses = [r.status_code for r in responses]
    assert statuses.count(200) == 1, (
        f"Ожидался ровно один успешный register, получили статусы: {statuses}"
    )
    assert all(s in (200, 400) for s in statuses), (
        f"Гонка на дублирующийся email уронила бэк в 500: {statuses}\n"
        f"Тела ответов: {[r.text[:150] for r in responses]}"
    )


def test_login_success():
    u = register_user()
    r = requests.post(f"{BASE_URL}/auth/login", json={
        "email": u["email"], "password": u["password"],
    })
    assert r.status_code == 200, r.text
    assert "access_token" in r.json()


def test_login_wrong_password():
    u = register_user()
    r = requests.post(f"{BASE_URL}/auth/login", json={
        "email": u["email"], "password": "wrong-password",
    })
    assert r.status_code == 401, r.text


def test_login_nonexistent_user():
    r = requests.post(f"{BASE_URL}/auth/login", json={
        "email": unique_email(), "password": "whatever",
    })
    assert r.status_code == 401, r.text


def test_login_case_sensitivity_email():
    u = register_user()
    r = requests.post(f"{BASE_URL}/auth/login", json={
        "email": u["email"].upper(), "password": u["password"],
    })
    # Документируем реальное поведение (может быть 200 или 401 в зависимости от чувствительности к регистру)
    assert r.status_code in (200, 401), r.text


def test_login_missing_fields():
    r = requests.post(f"{BASE_URL}/auth/login", json={"email": unique_email()})
    assert r.status_code == 422


# ── /auth/me и токены ────────────────────────────────────────────────────

def test_me_success():
    u = register_user()
    r = requests.get(f"{BASE_URL}/auth/me", headers=auth_headers(u["token"]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["email"] == u["email"]
    assert body["role"] == "lawyer"
    assert body["company_id"] is not None


def test_me_no_token():
    r = requests.get(f"{BASE_URL}/auth/me")
    assert r.status_code == 401, r.text


def test_me_malformed_auth_header():
    r = requests.get(f"{BASE_URL}/auth/me", headers={"Authorization": "not-a-bearer-token"})
    assert r.status_code == 401, r.text


def test_me_garbage_token():
    r = requests.get(f"{BASE_URL}/auth/me", headers=auth_headers("garbage.token.value"))
    # ВАЖНО: decode_token() в services/auth.py не оборачивает jwt.decode в try/except —
    # ожидаем 401, а не 500.
    assert r.status_code == 401, (
        f"Ожидался 401 для битого токена, получено {r.status_code}: {r.text}\n"
        f"Похоже decode_token() не ловит JWTError и роняет сервер в 500."
    )


def test_me_expired_token():
    u = register_user()
    me = requests.get(f"{BASE_URL}/auth/me", headers=auth_headers(u["token"])).json()
    expired = make_expired_token(me["id"])
    r = requests.get(f"{BASE_URL}/auth/me", headers=auth_headers(expired))
    assert r.status_code == 401, (
        f"Ожидался 401 для протухшего токена, получено {r.status_code}: {r.text}"
    )


def test_me_token_wrong_signature():
    u = register_user()
    me = requests.get(f"{BASE_URL}/auth/me", headers=auth_headers(u["token"])).json()
    bad_token = make_token(me["id"], secret="totally-wrong-secret")
    r = requests.get(f"{BASE_URL}/auth/me", headers=auth_headers(bad_token))
    assert r.status_code == 401, (
        f"Ожидался 401 для токена с неверной подписью, получено {r.status_code}: {r.text}"
    )


def test_me_token_for_nonexistent_user():
    fake_user_id = str(uuid.uuid4())
    token = make_token(fake_user_id)
    r = requests.get(f"{BASE_URL}/auth/me", headers=auth_headers(token))
    assert r.status_code == 401, r.text


def test_me_lowercase_bearer_scheme():
    u = register_user()
    r = requests.get(f"{BASE_URL}/auth/me", headers={"Authorization": f"bearer {u['token']}"})
    # Документируем реальное поведение OAuth2PasswordBearer к регистру схемы
    assert r.status_code in (200, 401), r.text


def test_me_extra_whitespace_in_auth_header():
    u = register_user()
    r = requests.get(f"{BASE_URL}/auth/me", headers={"Authorization": f"Bearer  {u['token']}"})
    assert r.status_code in (200, 401), r.text


def test_me_token_none_algorithm_attack():
    """Классическая атака alg=none — токен без подписи вообще."""
    import base64
    import json as _json
    header = base64.urlsafe_b64encode(_json.dumps({"alg": "none", "typ": "JWT"}).encode()).rstrip(b"=")
    payload = base64.urlsafe_b64encode(_json.dumps({"sub": str(uuid.uuid4())}).encode()).rstrip(b"=")
    forged = header.decode() + "." + payload.decode() + "."
    r = requests.get(f"{BASE_URL}/auth/me", headers=auth_headers(forged))
    assert r.status_code == 401, (
        f"КРИТИЧНО: alg=none токен должен быть отклонён, получено {r.status_code}: {r.text}"
    )
