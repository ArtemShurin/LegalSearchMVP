"""
Пробелы, найденные после первого прогона: проверка длины строк относительно
лимитов колонок в БД (VARCHAR(n)) и несоответствия типов полей — ни то,
ни другое не дублируется в Pydantic-схемах, значит бэк может упасть на
уровне БД, а не на уровне валидации.

Лимиты из models/*.py:
  Company.name            String(255)
  Collection.name         String(255)
  CollectionItem.document_id String(255)
  CollectionItem.title    String(500)
  User.email               String(255)
"""
import requests

from conftest import BASE_URL, auth_headers, register_user, unique_email


# ── Превышение длины строки относительно VARCHAR(n) в БД ────────────────

def test_register_company_name_exceeds_varchar_255():
    r = requests.post(f"{BASE_URL}/auth/register", json={
        "email": unique_email(), "password": "pass", "company_name": "X" * 1000,
    })
    assert r.status_code == 422, r.text


def test_collection_name_exceeds_varchar_255():
    u = register_user()
    r = requests.post(f"{BASE_URL}/collections/", json={"name": "X" * 1000},
                       headers=auth_headers(u["token"]))
    assert r.status_code == 422, r.text


def test_collection_item_document_id_exceeds_varchar_255():
    u = register_user()
    col = requests.post(f"{BASE_URL}/collections/", json={"name": "T"},
                         headers=auth_headers(u["token"])).json()
    r = requests.post(f"{BASE_URL}/collections/{col['id']}/items",
                       json={"document_id": "X" * 1000, "title": "T"},
                       headers=auth_headers(u["token"]))
    assert r.status_code == 422, r.text


def test_collection_item_title_exceeds_varchar_500():
    u = register_user()
    col = requests.post(f"{BASE_URL}/collections/", json={"name": "T"},
                         headers=auth_headers(u["token"])).json()
    r = requests.post(f"{BASE_URL}/collections/{col['id']}/items",
                       json={"document_id": "doc", "title": "X" * 2000},
                       headers=auth_headers(u["token"]))
    assert r.status_code == 422, r.text


def test_register_email_exceeds_varchar_255():
    long_local_part = "a" * 250
    r = requests.post(f"{BASE_URL}/auth/register", json={
        "email": f"{long_local_part}@backendtest.io", "password": "pass", "company_name": "X",
    })
    assert r.status_code in (200, 400, 422), (
        f"email > 255 символов уронил бэк: {r.status_code} {r.text[:300]}"
    )


# ── Несоответствие типов полей ───────────────────────────────────────────

def test_search_query_as_number():
    u = register_user()
    r = requests.post(f"{BASE_URL}/search/", json={"query": 12345},
                       headers=auth_headers(u["token"]))
    assert r.status_code == 422, r.text


def test_search_query_as_array():
    u = register_user()
    r = requests.post(f"{BASE_URL}/search/", json={"query": ["a", "b"]},
                       headers=auth_headers(u["token"]))
    assert r.status_code == 422, r.text


def test_search_query_as_null():
    u = register_user()
    r = requests.post(f"{BASE_URL}/search/", json={"query": None},
                       headers=auth_headers(u["token"]))
    assert r.status_code == 422, r.text


def test_search_filters_as_string_instead_of_object():
    u = register_user()
    r = requests.post(f"{BASE_URL}/search/", json={"query": "test", "filters": "not-an-object"},
                       headers=auth_headers(u["token"]))
    assert r.status_code == 422, r.text


def test_register_email_as_number():
    r = requests.post(f"{BASE_URL}/auth/register", json={
        "email": 12345, "password": "pass", "company_name": "X",
    })
    assert r.status_code == 422, r.text


def test_register_password_as_number():
    r = requests.post(f"{BASE_URL}/auth/register", json={
        "email": unique_email(), "password": 12345, "company_name": "X",
    })
    # pydantic v2 по умолчанию НЕ коэрсит int -> str в lax-режиме для str-полей
    assert r.status_code == 422, r.text


def test_register_company_name_as_array():
    r = requests.post(f"{BASE_URL}/auth/register", json={
        "email": unique_email(), "password": "pass", "company_name": ["X", "Y"],
    })
    assert r.status_code == 422, r.text


def test_collection_name_as_number():
    u = register_user()
    r = requests.post(f"{BASE_URL}/collections/", json={"name": 123},
                       headers=auth_headers(u["token"]))
    assert r.status_code == 422, r.text


def test_collection_item_document_id_as_number():
    u = register_user()
    col = requests.post(f"{BASE_URL}/collections/", json={"name": "T"},
                         headers=auth_headers(u["token"])).json()
    r = requests.post(f"{BASE_URL}/collections/{col['id']}/items",
                       json={"document_id": 123, "title": "T"},
                       headers=auth_headers(u["token"]))
    assert r.status_code == 422, r.text


def test_login_password_as_object():
    r = requests.post(f"{BASE_URL}/auth/login", json={
        "email": unique_email(), "password": {"nested": "object"},
    })
    assert r.status_code == 422, r.text


# ── Пустые/пробельные строки ──────────────────────────────────────────────

def test_register_whitespace_only_company_name():
    r = requests.post(f"{BASE_URL}/auth/register", json={
        "email": unique_email(), "password": "pass", "company_name": "   ",
    })
    assert r.status_code in (200, 400, 422), r.text


def test_collection_whitespace_only_name():
    u = register_user()
    r = requests.post(f"{BASE_URL}/collections/", json={"name": "   "},
                       headers=auth_headers(u["token"]))
    assert r.status_code in (200, 400, 422), r.text


def test_search_whitespace_only_query():
    u = register_user()
    r = requests.post(f"{BASE_URL}/search/", json={"query": "   "},
                       headers=auth_headers(u["token"]))
    assert r.status_code in (200, 400, 422), r.text


# ── Null/пустое тело запроса ──────────────────────────────────────────────

def test_register_null_body():
    r = requests.post(f"{BASE_URL}/auth/register", data="null",
                       headers={"Content-Type": "application/json"})
    assert r.status_code == 422, r.text


def test_search_empty_body():
    u = register_user()
    r = requests.post(f"{BASE_URL}/search/", data="",
                       headers={"Content-Type": "application/json", **auth_headers(u["token"])})
    assert r.status_code == 422, r.text


def test_collections_array_instead_of_object_body():
    u = register_user()
    r = requests.post(f"{BASE_URL}/collections/", json=["not", "an", "object"],
                       headers=auth_headers(u["token"]))
    assert r.status_code == 422, r.text
