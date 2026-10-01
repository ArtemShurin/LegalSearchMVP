import json

import requests

from conftest import BASE_URL, auth_headers, register_user


def parse_sse(text: str) -> dict:
    """/search/ теперь отдаёт SSE (events: sources, token*, done), а не единый
    JSON — разбираем поток в тот же плоский вид, что был раньше у r.json()."""
    sources, answer_parts, query_id = [], [], None
    event = None
    for line in text.split("\n"):
        if line.startswith("event:"):
            event = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data = json.loads(line[len("data:"):].strip())
            if event == "sources":
                sources = data["sources"]
            elif event == "token":
                answer_parts.append(data["text"])
            elif event == "done":
                query_id = data["query_id"]
    return {"query_id": query_id, "answer": "".join(answer_parts), "sources": sources}


def test_search_no_auth():
    r = requests.post(f"{BASE_URL}/search/", json={"query": "взыскание убытков"})
    assert r.status_code == 401, r.text


def test_search_basic_success():
    u = register_user()
    r = requests.post(f"{BASE_URL}/search/", json={"query": "взыскание убытков по SLA"},
                       headers=auth_headers(u["token"]))
    assert r.status_code == 200, r.text
    body = parse_sse(r.text)
    assert body["query_id"] is not None
    assert "answer" in body
    assert isinstance(body["sources"], list)


def test_search_with_filters():
    u = register_user()
    r = requests.post(f"{BASE_URL}/search/", json={
        "query": "налоговый спор",
        "filters": {
            "court": "АС города Москвы",
            "region": "Москва",
            "date_from": "2023-01-01",
            "date_to": "2024-01-01",
            "category": "tax",
        },
    }, headers=auth_headers(u["token"]))
    assert r.status_code == 200, r.text


def test_search_empty_query_string():
    u = register_user()
    r = requests.post(f"{BASE_URL}/search/", json={"query": ""},
                       headers=auth_headers(u["token"]))
    # Пустая строка формально проходит pydantic-валидацию (просто str) —
    # проверяем что бэк не падает 500
    assert r.status_code in (200, 400, 422), r.text


def test_search_missing_query_field():
    u = register_user()
    r = requests.post(f"{BASE_URL}/search/", json={},
                       headers=auth_headers(u["token"]))
    assert r.status_code == 422, r.text


def test_search_very_long_query():
    u = register_user()
    long_query = "неустойка по договору поставки " * 2000  # ~64000 символов
    r = requests.post(f"{BASE_URL}/search/", json={"query": long_query},
                       headers=auth_headers(u["token"]))
    assert r.status_code in (200, 400, 413, 422), (
        f"Очень длинный запрос уронил бэк: {r.status_code} {r.text[:300]}"
    )


def test_search_unicode_and_special_chars():
    u = register_user()
    r = requests.post(f"{BASE_URL}/search/", json={"query": "§15 ГК РФ — «убытки» 100% ½ 🏛 <script>alert(1)</script>"},
                       headers=auth_headers(u["token"]))
    assert r.status_code == 200, r.text


def test_search_null_filters_explicit():
    u = register_user()
    r = requests.post(f"{BASE_URL}/search/", json={"query": "test", "filters": None},
                       headers=auth_headers(u["token"]))
    assert r.status_code == 200, r.text


def test_search_partial_filters():
    u = register_user()
    r = requests.post(f"{BASE_URL}/search/", json={
        "query": "test", "filters": {"category": "bankruptcy"},
    }, headers=auth_headers(u["token"]))
    assert r.status_code == 200, r.text


def test_search_saves_to_history():
    u = register_user()
    search_resp = requests.post(f"{BASE_URL}/search/", json={"query": "уникальный маркерный запрос XYZ123"},
                                 headers=auth_headers(u["token"]))
    assert search_resp.status_code == 200

    hist = requests.get(f"{BASE_URL}/history/", headers=auth_headers(u["token"]))
    assert hist.status_code == 200
    texts = [q["text"] for q in hist.json()]
    assert "уникальный маркерный запрос XYZ123" in texts


def test_search_response_saved_sources_match():
    u = register_user()
    search_resp = requests.post(f"{BASE_URL}/search/", json={"query": "проверка источников"},
                                 headers=auth_headers(u["token"]))
    assert search_resp.status_code == 200
    returned_sources = parse_sse(search_resp.text)["sources"]

    hist = requests.get(f"{BASE_URL}/history/", headers=auth_headers(u["token"])).json()
    saved = next(q for q in hist if q["text"] == "проверка источников")
    assert len(saved["sources"]) == len(returned_sources)


def test_search_wrong_filter_types():
    u = register_user()
    r = requests.post(f"{BASE_URL}/search/", json={
        "query": "test", "filters": {"date_from": 12345},  # число вместо строки
    }, headers=auth_headers(u["token"]))
    assert r.status_code == 422, r.text


def test_search_extra_unknown_fields_ignored():
    u = register_user()
    r = requests.post(f"{BASE_URL}/search/", json={
        "query": "test", "unexpected_field": "value", "another": 123,
    }, headers=auth_headers(u["token"]))
    assert r.status_code == 200, r.text
