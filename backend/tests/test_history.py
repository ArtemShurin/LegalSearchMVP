import requests

from conftest import BASE_URL, auth_headers, register_user


def test_history_no_auth():
    r = requests.get(f"{BASE_URL}/history/")
    assert r.status_code == 401, r.text


def test_history_empty_for_new_user():
    u = register_user()
    r = requests.get(f"{BASE_URL}/history/", headers=auth_headers(u["token"]))
    assert r.status_code == 200, r.text
    assert r.json() == []


def test_history_isolated_between_users():
    a = register_user()
    b = register_user()

    requests.post(f"{BASE_URL}/search/", json={"query": "запрос пользователя A"},
                   headers=auth_headers(a["token"]))

    hist_a = requests.get(f"{BASE_URL}/history/", headers=auth_headers(a["token"])).json()
    hist_b = requests.get(f"{BASE_URL}/history/", headers=auth_headers(b["token"])).json()

    assert any(q["text"] == "запрос пользователя A" for q in hist_a)
    assert not any(q["text"] == "запрос пользователя A" for q in hist_b), (
        "УТЕЧКА ДАННЫХ: история пользователя A видна пользователю B"
    )


def test_history_ordering_newest_first():
    u = register_user()
    for text in ["первый запрос", "второй запрос", "третий запрос"]:
        r = requests.post(f"{BASE_URL}/search/", json={"query": text},
                           headers=auth_headers(u["token"]))
        assert r.status_code == 200

    hist = requests.get(f"{BASE_URL}/history/", headers=auth_headers(u["token"])).json()
    ours = [q["text"] for q in hist if q["text"] in ("первый запрос", "второй запрос", "третий запрос")]
    assert ours == ["третий запрос", "второй запрос", "первый запрос"], (
        f"История должна быть отсортирована от новых к старым, получили: {ours}"
    )


def test_history_respects_limit_of_50():
    """get_user_history() захардкожен на limit=50 — проверяем что реально не отдаёт больше."""
    u = register_user()
    for i in range(60):
        r = requests.post(f"{BASE_URL}/search/", json={"query": f"лимит-запрос-{i:03d}"},
                           headers=auth_headers(u["token"]))
        assert r.status_code == 200

    hist = requests.get(f"{BASE_URL}/history/", headers=auth_headers(u["token"])).json()
    assert len(hist) <= 50, f"История вернула больше 50 записей: {len(hist)}"

    # и это должны быть именно САМЫЕ ПОСЛЕДНИЕ 50 (не первые 50)
    texts = [q["text"] for q in hist]
    assert "лимит-запрос-059" in texts, "Самый последний запрос должен попадать в историю"
    assert "лимит-запрос-000" not in texts, "Самый первый из 60 запросов не должен попадать в limit=50"


def test_history_response_shape():
    u = register_user()
    requests.post(f"{BASE_URL}/search/", json={"query": "проверка формы ответа"},
                  headers=auth_headers(u["token"]))
    hist = requests.get(f"{BASE_URL}/history/", headers=auth_headers(u["token"])).json()
    item = next(q for q in hist if q["text"] == "проверка формы ответа")
    for field in ("id", "text", "answer", "created_at", "sources"):
        assert field in item, f"В истории отсутствует поле {field}: {item}"
    assert isinstance(item["sources"], list)
