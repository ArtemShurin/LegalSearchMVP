import uuid

import requests

from conftest import BASE_URL, auth_headers, register_user


def create_collection(token: str, name: str = "Тестовая подборка") -> dict:
    r = requests.post(f"{BASE_URL}/collections/", json={"name": name}, headers=auth_headers(token))
    assert r.status_code == 200, r.text
    return r.json()


# ── Базовый happy path ───────────────────────────────────────────────────

def test_collections_no_auth():
    assert requests.get(f"{BASE_URL}/collections/").status_code == 401
    assert requests.post(f"{BASE_URL}/collections/", json={"name": "x"}).status_code == 401


def test_collections_empty_for_new_user():
    u = register_user()
    r = requests.get(f"{BASE_URL}/collections/", headers=auth_headers(u["token"]))
    assert r.status_code == 200
    assert r.json() == []


def test_create_collection():
    u = register_user()
    col = create_collection(u["token"], "Дело Иванова")
    assert col["name"] == "Дело Иванова"
    assert col["items"] == []
    assert "id" in col


def test_create_collection_missing_name():
    u = register_user()
    r = requests.post(f"{BASE_URL}/collections/", json={}, headers=auth_headers(u["token"]))
    assert r.status_code == 422


def test_create_collection_empty_name():
    u = register_user()
    r = requests.post(f"{BASE_URL}/collections/", json={"name": ""}, headers=auth_headers(u["token"]))
    assert r.status_code in (200, 400, 422), r.text


def test_list_collections_after_create():
    u = register_user()
    create_collection(u["token"], "Подборка 1")
    create_collection(u["token"], "Подборка 2")
    r = requests.get(f"{BASE_URL}/collections/", headers=auth_headers(u["token"]))
    names = [c["name"] for c in r.json()]
    assert "Подборка 1" in names
    assert "Подборка 2" in names


def test_rename_collection():
    u = register_user()
    col = create_collection(u["token"], "Старое имя")
    r = requests.patch(f"{BASE_URL}/collections/{col['id']}", json={"name": "Новое имя"},
                        headers=auth_headers(u["token"]))
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "Новое имя"
    assert r.json()["id"] == col["id"]

    got = requests.get(f"{BASE_URL}/collections/", headers=auth_headers(u["token"])).json()
    target = next(c for c in got if c["id"] == col["id"])
    assert target["name"] == "Новое имя"


def test_rename_collection_preserves_items():
    u = register_user()
    col = create_collection(u["token"])
    requests.post(f"{BASE_URL}/collections/{col['id']}/items",
                  json={"document_id": "doc", "title": "T"}, headers=auth_headers(u["token"]))

    r = requests.patch(f"{BASE_URL}/collections/{col['id']}", json={"name": "Переименовано"},
                        headers=auth_headers(u["token"]))
    assert r.status_code == 200, r.text
    assert len(r.json()["items"]) == 1


def test_rename_collection_missing_name():
    u = register_user()
    col = create_collection(u["token"])
    r = requests.patch(f"{BASE_URL}/collections/{col['id']}", json={},
                        headers=auth_headers(u["token"]))
    assert r.status_code == 422, r.text


def test_rename_collection_empty_name():
    u = register_user()
    col = create_collection(u["token"])
    r = requests.patch(f"{BASE_URL}/collections/{col['id']}", json={"name": ""},
                        headers=auth_headers(u["token"]))
    assert r.status_code in (200, 400, 422), r.text


def test_rename_collection_exceeds_varchar_255():
    u = register_user()
    col = create_collection(u["token"])
    r = requests.patch(f"{BASE_URL}/collections/{col['id']}", json={"name": "X" * 1000},
                        headers=auth_headers(u["token"]))
    assert r.status_code == 422, r.text


def test_rename_nonexistent_collection():
    u = register_user()
    r = requests.patch(f"{BASE_URL}/collections/{uuid.uuid4()}", json={"name": "X"},
                        headers=auth_headers(u["token"]))
    assert r.status_code == 404, r.text


def test_rename_malformed_uuid():
    u = register_user()
    r = requests.patch(f"{BASE_URL}/collections/not-a-valid-uuid", json={"name": "X"},
                        headers=auth_headers(u["token"]))
    assert r.status_code in (400, 404, 422), (
        f"Битый UUID в PATCH уронил бэк с {r.status_code}: {r.text[:300]}"
    )


def test_rename_no_auth():
    col_owner = register_user()
    col = create_collection(col_owner["token"])
    r = requests.patch(f"{BASE_URL}/collections/{col['id']}", json={"name": "X"})
    assert r.status_code == 401, r.text


def test_user_b_cannot_rename_user_a_collection():
    a = register_user()
    b = register_user()
    col = create_collection(a["token"], "Оригинал")

    r = requests.patch(f"{BASE_URL}/collections/{col['id']}", json={"name": "Хакнуто"},
                        headers=auth_headers(b["token"]))
    assert r.status_code == 404, (
        f"КРИТИЧНО: пользователь B смог переименовать чужую подборку, статус {r.status_code}"
    )

    still = requests.get(f"{BASE_URL}/collections/", headers=auth_headers(a["token"])).json()
    target = next(c for c in still if c["id"] == col["id"])
    assert target["name"] == "Оригинал"


def test_add_item_to_collection():
    u = register_user()
    col = create_collection(u["token"])
    r = requests.post(f"{BASE_URL}/collections/{col['id']}/items",
                       json={"document_id": "А40-123456/2023", "title": "Постановление АС Москвы"},
                       headers=auth_headers(u["token"]))
    assert r.status_code == 200, r.text

    got = requests.get(f"{BASE_URL}/collections/", headers=auth_headers(u["token"])).json()
    target = next(c for c in got if c["id"] == col["id"])
    assert len(target["items"]) == 1
    assert target["items"][0]["document_id"] == "А40-123456/2023"


def test_add_duplicate_document_id_rejected():
    u = register_user()
    col = create_collection(u["token"])
    first = requests.post(f"{BASE_URL}/collections/{col['id']}/items",
                           json={"document_id": "dup-doc", "title": "T1"},
                           headers=auth_headers(u["token"]))
    assert first.status_code == 200

    second = requests.post(f"{BASE_URL}/collections/{col['id']}/items",
                            json={"document_id": "dup-doc", "title": "T2 другое название"},
                            headers=auth_headers(u["token"]))
    assert second.status_code == 409, second.text
    assert "уже добавлен" in second.json()["detail"].lower()

    got = requests.get(f"{BASE_URL}/collections/", headers=auth_headers(u["token"])).json()
    target = next(c for c in got if c["id"] == col["id"])
    assert len(target["items"]) == 1, "Дубликат не должен был создать вторую запись"


def test_readd_document_after_deletion_allowed():
    """Удалили элемент из подборки — тот же document_id можно добавить заново,
    и физически создаётся новая строка (id/added_at другие), а не 409."""
    u = register_user()
    col = create_collection(u["token"])
    add1 = requests.post(f"{BASE_URL}/collections/{col['id']}/items",
                          json={"document_id": "doc-x", "title": "v1"},
                          headers=auth_headers(u["token"]))
    assert add1.status_code == 200

    listing = requests.get(f"{BASE_URL}/collections/", headers=auth_headers(u["token"])).json()
    first_item = next(c for c in listing if c["id"] == col["id"])["items"][0]

    delr = requests.delete(f"{BASE_URL}/collections/{col['id']}/items/{first_item['id']}",
                            headers=auth_headers(u["token"]))
    assert delr.status_code == 200

    add2 = requests.post(f"{BASE_URL}/collections/{col['id']}/items",
                          json={"document_id": "doc-x", "title": "v2"},
                          headers=auth_headers(u["token"]))
    assert add2.status_code == 200, (
        f"Повторное добавление того же document_id после удаления должно быть разрешено: {add2.text}"
    )

    after = requests.get(f"{BASE_URL}/collections/", headers=auth_headers(u["token"])).json()
    items = next(c for c in after if c["id"] == col["id"])["items"]
    assert len(items) == 1
    assert items[0]["id"] != first_item["id"], "Должна быть создана новая строка, а не восстановлена старая"
    assert items[0]["title"] == "v2"


def test_same_document_id_allowed_in_different_collections():
    u = register_user()
    col1 = create_collection(u["token"], "Подборка 1")
    col2 = create_collection(u["token"], "Подборка 2")

    r1 = requests.post(f"{BASE_URL}/collections/{col1['id']}/items",
                        json={"document_id": "shared-doc", "title": "T"},
                        headers=auth_headers(u["token"]))
    r2 = requests.post(f"{BASE_URL}/collections/{col2['id']}/items",
                        json={"document_id": "shared-doc", "title": "T"},
                        headers=auth_headers(u["token"]))
    assert r1.status_code == 200
    assert r2.status_code == 200, "Один и тот же document_id должен быть разрешён в разных подборках"


def test_duplicate_collection_names_allowed_for_same_user():
    u = register_user()
    col1 = create_collection(u["token"], "Одинаковое имя")
    col2 = create_collection(u["token"], "Одинаковое имя")
    assert col1["id"] != col2["id"]

    got = requests.get(f"{BASE_URL}/collections/", headers=auth_headers(u["token"])).json()
    same_name = [c for c in got if c["name"] == "Одинаковое имя"]
    assert len(same_name) == 2, "Две подборки с одинаковым именем должны сосуществовать"


def test_large_number_of_items_in_collection():
    u = register_user()
    col = create_collection(u["token"])
    for i in range(120):
        r = requests.post(f"{BASE_URL}/collections/{col['id']}/items",
                           json={"document_id": f"vol-doc-{i:04d}", "title": f"Title {i}"},
                           headers=auth_headers(u["token"]))
        assert r.status_code == 200, f"Упало на элементе {i}: {r.status_code} {r.text[:200]}"

    got = requests.get(f"{BASE_URL}/collections/", headers=auth_headers(u["token"]))
    assert got.status_code == 200, (
        f"GET /collections/ не выдержал 120 элементов в подборке: {got.status_code}"
    )
    target = next(c for c in got.json() if c["id"] == col["id"])
    assert len(target["items"]) == 120


def test_add_multiple_items_to_collection():
    u = register_user()
    col = create_collection(u["token"])
    for i in range(5):
        r = requests.post(f"{BASE_URL}/collections/{col['id']}/items",
                           json={"document_id": f"doc-{i}", "title": f"Title {i}"},
                           headers=auth_headers(u["token"]))
        assert r.status_code == 200

    got = requests.get(f"{BASE_URL}/collections/", headers=auth_headers(u["token"])).json()
    target = next(c for c in got if c["id"] == col["id"])
    assert len(target["items"]) == 5


def test_add_item_missing_fields():
    u = register_user()
    col = create_collection(u["token"])
    r = requests.post(f"{BASE_URL}/collections/{col['id']}/items",
                       json={"document_id": "x"}, headers=auth_headers(u["token"]))
    assert r.status_code == 422


def test_delete_collection_item():
    u = register_user()
    col = create_collection(u["token"])
    add = requests.post(f"{BASE_URL}/collections/{col['id']}/items",
                         json={"document_id": "doc-to-delete", "title": "T"},
                         headers=auth_headers(u["token"]))
    assert add.status_code == 200

    listing = requests.get(f"{BASE_URL}/collections/", headers=auth_headers(u["token"])).json()
    item = next(c for c in listing if c["id"] == col["id"])["items"][0]

    r = requests.delete(f"{BASE_URL}/collections/{col['id']}/items/{item['id']}",
                         headers=auth_headers(u["token"]))
    assert r.status_code == 200, r.text

    after = requests.get(f"{BASE_URL}/collections/", headers=auth_headers(u["token"])).json()
    target = next(c for c in after if c["id"] == col["id"])
    assert target["items"] == []


def test_delete_nonexistent_item():
    u = register_user()
    col = create_collection(u["token"])
    fake_item_id = str(uuid.uuid4())
    r = requests.delete(f"{BASE_URL}/collections/{col['id']}/items/{fake_item_id}",
                         headers=auth_headers(u["token"]))
    assert r.status_code == 404, r.text


def test_delete_collection():
    u = register_user()
    col = create_collection(u["token"])
    r = requests.delete(f"{BASE_URL}/collections/{col['id']}", headers=auth_headers(u["token"]))
    assert r.status_code == 200, r.text

    after = requests.get(f"{BASE_URL}/collections/", headers=auth_headers(u["token"])).json()
    assert col["id"] not in [c["id"] for c in after]


def test_delete_collection_with_items_does_not_crash():
    """Удаление подборки, в которой есть элементы — раньше падало 500 (FK violation)."""
    u = register_user()
    col = create_collection(u["token"])
    requests.post(f"{BASE_URL}/collections/{col['id']}/items",
                  json={"document_id": "d1", "title": "T1"}, headers=auth_headers(u["token"]))
    requests.post(f"{BASE_URL}/collections/{col['id']}/items",
                  json={"document_id": "d2", "title": "T2"}, headers=auth_headers(u["token"]))

    r = requests.delete(f"{BASE_URL}/collections/{col['id']}", headers=auth_headers(u["token"]))
    assert r.status_code == 200, r.text

    after = requests.get(f"{BASE_URL}/collections/", headers=auth_headers(u["token"])).json()
    assert col["id"] not in [c["id"] for c in after]


def test_delete_nonexistent_collection():
    u = register_user()
    r = requests.delete(f"{BASE_URL}/collections/{uuid.uuid4()}", headers=auth_headers(u["token"]))
    assert r.status_code == 404, r.text


# ── Изоляция между пользователями ────────────────────────────────────────

def test_collection_isolated_between_users():
    a = register_user()
    b = register_user()
    create_collection(a["token"], "Приватная подборка A")

    listing_b = requests.get(f"{BASE_URL}/collections/", headers=auth_headers(b["token"])).json()
    assert not any(c["name"] == "Приватная подборка A" for c in listing_b), (
        "УТЕЧКА ДАННЫХ: подборка пользователя A видна пользователю B"
    )


def test_user_b_cannot_add_item_to_user_a_collection():
    a = register_user()
    b = register_user()
    col = create_collection(a["token"])

    r = requests.post(f"{BASE_URL}/collections/{col['id']}/items",
                       json={"document_id": "hack", "title": "hack"},
                       headers=auth_headers(b["token"]))
    assert r.status_code == 404, (
        f"КРИТИЧНО: пользователь B смог добавить элемент в чужую подборку, статус {r.status_code}"
    )


def test_user_b_cannot_delete_user_a_collection():
    a = register_user()
    b = register_user()
    col = create_collection(a["token"])

    r = requests.delete(f"{BASE_URL}/collections/{col['id']}", headers=auth_headers(b["token"]))
    assert r.status_code == 404, (
        f"КРИТИЧНО: пользователь B смог удалить чужую подборку, статус {r.status_code}"
    )

    # и подборка A реально жива
    still_there = requests.get(f"{BASE_URL}/collections/", headers=auth_headers(a["token"])).json()
    assert any(c["id"] == col["id"] for c in still_there)


def test_user_b_cannot_delete_item_from_user_a_collection():
    a = register_user()
    b = register_user()
    col = create_collection(a["token"])
    requests.post(f"{BASE_URL}/collections/{col['id']}/items",
                  json={"document_id": "doc", "title": "T"}, headers=auth_headers(a["token"]))
    item = requests.get(f"{BASE_URL}/collections/", headers=auth_headers(a["token"])).json()
    item_id = next(c for c in item if c["id"] == col["id"])["items"][0]["id"]

    r = requests.delete(f"{BASE_URL}/collections/{col['id']}/items/{item_id}",
                         headers=auth_headers(b["token"]))
    assert r.status_code == 404, (
        f"КРИТИЧНО: пользователь B смог удалить элемент чужой подборки, статус {r.status_code}"
    )


# ── Некорректные ID (потенциальные 500) ──────────────────────────────────

def test_malformed_uuid_in_collection_id_get_items():
    u = register_user()
    r = requests.post(f"{BASE_URL}/collections/not-a-valid-uuid/items",
                       json={"document_id": "x", "title": "y"},
                       headers=auth_headers(u["token"]))
    assert r.status_code in (400, 404, 422), (
        f"Битый UUID в пути уронил бэк с {r.status_code} (ожидали 400/404/422): {r.text[:300]}"
    )


def test_malformed_uuid_in_delete_collection():
    u = register_user()
    r = requests.delete(f"{BASE_URL}/collections/not-a-valid-uuid",
                         headers=auth_headers(u["token"]))
    assert r.status_code in (400, 404, 422), (
        f"Битый UUID в пути уронил бэк с {r.status_code} (ожидали 400/404/422): {r.text[:300]}"
    )


def test_malformed_uuid_in_delete_item():
    u = register_user()
    col = create_collection(u["token"])
    r = requests.delete(f"{BASE_URL}/collections/{col['id']}/items/not-a-valid-uuid",
                         headers=auth_headers(u["token"]))
    assert r.status_code in (400, 404, 422), (
        f"Битый UUID айтема уронил бэк с {r.status_code} (ожидали 400/404/422): {r.text[:300]}"
    )


def test_sql_injection_in_document_id():
    u = register_user()
    col = create_collection(u["token"])
    r = requests.post(f"{BASE_URL}/collections/{col['id']}/items",
                       json={"document_id": "'; DROP TABLE collections; --", "title": "T"},
                       headers=auth_headers(u["token"]))
    assert r.status_code == 200, r.text
    # проверяем что таблица жива
    still = requests.get(f"{BASE_URL}/collections/", headers=auth_headers(u["token"]))
    assert still.status_code == 200
