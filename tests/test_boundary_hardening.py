"""Web and AI boundary checks: AI call-limit configuration, validation-error echoing,
cookie-write origin evidence, and the API documentation surface.

AI tests use httpx.MockTransport only; no live provider is contacted.
"""

from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from foundry.api import create_app

ORIGIN = "http://testserver"


def ai_body():
    return {
        "question": "Which assumptions should we test first?",
        "share_with_provider": True,
        "max_output_tokens": 1000,
    }


# 1. An invalid AI call limit fails closed with a normal refusal.


@pytest.mark.parametrize("value", ["abc", "-1", "2.5", "", " ", "1e3", "+5", "5_0"])
def test_invalid_ai_daily_limit_refuses_without_provider_call_or_reservation(setup, monkeypatch, value):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(500, json={})

    monkeypatch.setenv("OPENAI_API_KEY", "test-not-a-real-key")
    monkeypatch.setenv("FOUNDRY_OPENAI_MODEL", "example-model")
    monkeypatch.setenv("FOUNDRY_AI_DAILY_CALLS", value)
    bid = setup[3]
    app = create_app(setup[0].path, allowed_hosts=["testserver"], ai_transport=httpx.MockTransport(handler))
    with TestClient(app, headers={"Authorization": "Bearer " + setup[4]}) as c:
        response = c.post(
            "/api/businesses/" + bid + "/ai", headers={"Idempotency-Key": "ai-1"}, json=ai_body()
        )
        assert response.status_code == 503
        detail = response.json()["detail"]
        assert "FOUNDRY_AI_DAILY_CALLS" in detail
        assert "whole number" in detail and "0 or greater" in detail
        assert calls == []
        assert c.get("/api/businesses/" + bid + "/ai").json() == []
    with setup[0].read() as con:
        assert con.execute("SELECT COUNT(*) FROM ai_calls").fetchone()[0] == 0


def test_zero_ai_daily_limit_is_valid_and_blocks_calls(setup, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(500, json={})

    monkeypatch.setenv("OPENAI_API_KEY", "test-not-a-real-key")
    monkeypatch.setenv("FOUNDRY_OPENAI_MODEL", "example-model")
    monkeypatch.setenv("FOUNDRY_AI_DAILY_CALLS", " 0 ")
    app = create_app(setup[0].path, allowed_hosts=["testserver"], ai_transport=httpx.MockTransport(handler))
    with TestClient(app, headers={"Authorization": "Bearer " + setup[4]}) as c:
        response = c.post(
            "/api/businesses/" + setup[3] + "/ai", headers={"Idempotency-Key": "ai-0"}, json=ai_body()
        )
        assert response.status_code == 429
        assert calls == []


# 2. Validation errors do not echo client-invented field names.


def test_unexpected_top_level_field_name_is_not_echoed(client):
    response = client.post(
        "/api/businesses",
        headers={"Idempotency-Key": str(uuid4())},
        json={"name": "Valid", "concept": "A valid concept text.", "SYNTHETIC_SECRET_FIELD_NAME": "x"},
    )
    assert response.status_code == 422
    assert "SYNTHETIC_SECRET_FIELD_NAME" not in response.text
    assert response.json()["detail"] == "body: an unexpected field was sent"


def test_unexpected_nested_field_reports_defined_parent(client, setup):
    response = client.post(
        "/api/businesses/" + setup[3] + "/forecasts",
        headers={"Idempotency-Key": str(uuid4())},
        json={"inputs": {"months": 12, "SYNTHETIC_SECRET_FIELD_NAME": 1}},
    )
    assert response.status_code == 422
    assert "SYNTHETIC_SECRET_FIELD_NAME" not in response.text
    assert response.json()["detail"] == "body.inputs: an unexpected field was sent"


def test_defined_field_names_are_still_reported_without_values(client):
    secret = "SYNTHETIC-PRIVATE-VALUE"
    response = client.post(
        "/api/businesses",
        headers={"Idempotency-Key": str(uuid4())},
        json={"name": secret, "jurisdiction": secret},
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "body.concept" in detail and "body.jurisdiction" in detail
    assert secret not in response.text


def test_client_supplied_dictionary_keys_are_not_echoed(client, setup):
    response = client.post(
        "/api/businesses/" + setup[3] + "/records",
        headers={"Idempotency-Key": str(uuid4())},
        json={"kind": "customer", "title": "Record", "properties": {"SYNTHETIC_SECRET_KEY": [1]}},
    )
    assert response.status_code == 422
    assert "SYNTHETIC_SECRET_KEY" not in response.text
    assert "body.properties" in response.json()["detail"]


# 3. Cookie-authenticated writes require affirmative same-origin evidence.


def cookie_client(setup):
    app = create_app(setup[0].path, allowed_hosts=["testserver"])
    c = TestClient(app)
    login = c.post("/api/login", json={"token": setup[4]})
    assert login.status_code == 200 and "foundry_session" in c.cookies
    return c


def new_business(c, headers):
    return c.post(
        "/api/businesses",
        json={"name": "Cookie write", "concept": "A synthetic cookie write check."},
        headers={"Idempotency-Key": str(uuid4()), **headers},
    )


def test_cookie_write_without_origin_evidence_is_refused(setup):
    with cookie_client(setup) as c:
        response = new_business(c, {})
        assert response.status_code == 403
        assert "same-origin" in response.json()["detail"]
        assert new_business(c, {"Sec-Fetch-Site": "same-site"}).status_code == 403
        assert new_business(c, {"Sec-Fetch-Site": "none"}).status_code == 403
        assert c.post("/api/logout").status_code == 403
        # The session survived the refused logout.
        assert c.get("/api/businesses").status_code == 200


def test_cookie_write_with_same_origin_evidence_is_accepted(setup):
    with cookie_client(setup) as c:
        assert new_business(c, {"Origin": ORIGIN}).status_code == 201
        assert new_business(c, {"Sec-Fetch-Site": "same-origin"}).status_code == 201
        assert new_business(c, {"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"}).status_code == 201


def test_cookie_write_cross_origin_and_cross_site_still_refused(setup):
    with cookie_client(setup) as c:
        assert new_business(c, {"Origin": "https://evil.example"}).status_code == 403
        assert (
            new_business(c, {"Origin": "https://evil.example", "Sec-Fetch-Site": "same-origin"}).status_code
            == 403
        )
        assert new_business(c, {"Origin": ORIGIN, "Sec-Fetch-Site": "cross-site"}).status_code == 403


def test_bearer_writes_need_no_origin_evidence(setup):
    app = create_app(setup[0].path, allowed_hosts=["testserver"])
    with TestClient(app, headers={"Authorization": "Bearer " + setup[4]}) as c:
        assert new_business(c, {}).status_code == 201
        assert new_business(c, {"Origin": "https://evil.example"}).status_code == 403


def test_sign_in_works_from_command_line_and_browser(setup):
    app = create_app(setup[0].path, allowed_hosts=["testserver"])
    with TestClient(app) as cli:
        assert cli.post("/api/login", json={"token": setup[4]}).status_code == 200
    with TestClient(app) as browser:
        response = browser.post(
            "/api/login",
            json={"token": setup[4]},
            headers={"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"},
        )
        assert response.status_code == 200
        # Signing in again from the browser, now carrying a session cookie, still works.
        again = browser.post(
            "/api/login",
            json={"token": setup[4]},
            headers={"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"},
        )
        assert again.status_code == 200


# 4. The API documentation page is not served; the schema is.


def test_swagger_page_is_not_served_and_schema_is(setup):
    app = create_app(setup[0].path, allowed_hosts=["testserver"])
    with TestClient(app) as c:
        assert c.get("/api/docs").status_code == 404
        assert c.get("/api/docs/oauth2-redirect").status_code == 404
        assert c.get("/api/redoc").status_code == 404
        schema = c.get("/api/openapi.json")
        assert schema.status_code == 200
        body = schema.json()
        assert body["openapi"].startswith("3.") and "/api/businesses" in body["paths"]
