import requests

from conftest import BASE_URL, auth_headers, register_user


def test_root_endpoint():
    r = requests.get(f"{BASE_URL}/")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_unknown_route_404():
    r = requests.get(f"{BASE_URL}/this-route-does-not-exist")
    assert r.status_code == 404


def test_wrong_method_on_search():
    r = requests.get(f"{BASE_URL}/search/")
    assert r.status_code == 405


def test_wrong_method_on_auth_register():
    r = requests.get(f"{BASE_URL}/auth/register")
    assert r.status_code == 405


def test_trailing_slash_variants():
    # FastAPI по умолчанию чувствителен к слэшу — документируем поведение
    u = register_user()
    r1 = requests.get(f"{BASE_URL}/history", headers=auth_headers(u["token"]), allow_redirects=False)
    r2 = requests.get(f"{BASE_URL}/history/", headers=auth_headers(u["token"]))
    assert r2.status_code == 200
    assert r1.status_code in (200, 307, 404)


def test_openapi_schema_available():
    r = requests.get(f"{BASE_URL}/openapi.json")
    assert r.status_code == 200
    assert "paths" in r.json()


def test_docs_available():
    r = requests.get(f"{BASE_URL}/docs")
    assert r.status_code == 200


def test_cors_headers_present():
    u = register_user()
    r = requests.options(f"{BASE_URL}/search/", headers={
        "Origin": "http://localhost:3000",
        "Access-Control-Request-Method": "POST",
    })
    assert r.status_code in (200, 204), r.text
    assert r.headers.get("access-control-allow-origin") == "http://localhost:3000"


def test_cors_disallowed_origin_not_echoed():
    """CORS настроен на allow_origins=['http://localhost:3000'] — левый origin
    не должен получать Access-Control-Allow-Origin со своим значением."""
    r = requests.options(f"{BASE_URL}/search/", headers={
        "Origin": "http://evil.com",
        "Access-Control-Request-Method": "POST",
    })
    acao = r.headers.get("access-control-allow-origin")
    assert acao != "http://evil.com", (
        f"КРИТИЧНО: CORS отражает произвольный Origin обратно, "
        f"Access-Control-Allow-Origin={acao!r}"
    )


def test_cors_allowed_origin_echoed_on_actual_request():
    u = register_user()
    r = requests.post(f"{BASE_URL}/search/", json={"query": "cors test"},
                       headers={**auth_headers(u["token"]), "Origin": "http://localhost:3000"})
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == "http://localhost:3000"


def test_no_server_error_leaks_traceback():
    """Проверяем, что ни один эндпоинт не отдаёт голый traceback в теле ответа."""
    u = register_user()
    endpoints = [
        ("GET", "/history/"),
        ("GET", "/collections/"),
        ("GET", "/auth/me"),
    ]
    for method, path in endpoints:
        r = requests.request(method, f"{BASE_URL}{path}", headers=auth_headers(u["token"]))
        assert "Traceback" not in r.text, f"{method} {path} вернул traceback в ответе!"
