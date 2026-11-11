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


# 5. Review round: the AI call limit has one bounded rule, so no value can crash a request.


def ai_app(setup, monkeypatch, value, calls):
    def handler(request):
        calls.append(request)
        return httpx.Response(500, json={})

    monkeypatch.setenv("OPENAI_API_KEY", "test-not-a-real-key")
    monkeypatch.setenv("FOUNDRY_OPENAI_MODEL", "example-model")
    monkeypatch.setenv("FOUNDRY_AI_DAILY_CALLS", value)
    return create_app(setup[0].path, allowed_hosts=["testserver"], ai_transport=httpx.MockTransport(handler))


def ai_rows_and_events(store):
    with store.read() as con:
        return (
            con.execute("SELECT COUNT(*) FROM ai_calls").fetchone()[0],
            con.execute("SELECT COUNT(*) FROM events").fetchone()[0],
        )


@pytest.mark.parametrize(
    "value",
    ["0" * 5000 + "1", "9" * 5000, "1" + "0" * 5000, "100001", "999999", "1000000", "007", "00", "01"],
)
def test_out_of_range_or_padded_ai_daily_limit_refuses_without_crash(setup, monkeypatch, value):
    calls = []
    before = ai_rows_and_events(setup[0])
    app = ai_app(setup, monkeypatch, value, calls)
    with TestClient(app, headers={"Authorization": "Bearer " + setup[4]}, raise_server_exceptions=False) as c:
        response = c.post(
            "/api/businesses/" + setup[3] + "/ai", headers={"Idempotency-Key": "ai-big"}, json=ai_body()
        )
        assert response.status_code == 503
        detail = response.json()["detail"]
        assert "FOUNDRY_AI_DAILY_CALLS" in detail and "at most 100000" in detail
        assert "leading zero" in detail
        if len(value) > 6:
            assert value not in detail
    assert calls == []
    assert ai_rows_and_events(setup[0]) == before


def test_ai_daily_limit_maximum_is_accepted(setup, monkeypatch):
    calls = []
    app = ai_app(setup, monkeypatch, "100000", calls)
    with TestClient(app, headers={"Authorization": "Bearer " + setup[4]}) as c:
        response = c.post(
            "/api/businesses/" + setup[3] + "/ai", headers={"Idempotency-Key": "ai-max"}, json=ai_body()
        )
        assert response.status_code == 200
        # The mocked provider refuses; the request itself was allowed and reserved.
        assert response.json()["state"] == "failed"
    assert len(calls) == 1
    assert ai_rows_and_events(setup[0])[0] == 1


def test_corrected_ai_daily_limit_lets_the_same_key_proceed(setup, monkeypatch):
    calls = []
    before = ai_rows_and_events(setup[0])
    app = ai_app(setup, monkeypatch, "9" * 5000, calls)
    with TestClient(app, headers={"Authorization": "Bearer " + setup[4]}) as c:
        url, headers = "/api/businesses/" + setup[3] + "/ai", {"Idempotency-Key": "ai-retry"}
        assert c.post(url, headers=headers, json=ai_body()).status_code == 503
        assert calls == [] and ai_rows_and_events(setup[0]) == before
        monkeypatch.setenv("FOUNDRY_AI_DAILY_CALLS", "5")
        retried = c.post(url, headers=headers, json=ai_body())
        assert retried.status_code == 200 and retried.json()["state"] == "failed"
        assert len(calls) == 1
        # The same key now returns the recorded run without a second provider request.
        again = c.post(url, headers=headers, json=ai_body())
        assert again.json()["id"] == retried.json()["id"] and len(calls) == 1
    rows, events = ai_rows_and_events(setup[0])
    assert rows == 1 and events == before[1] + 2


# 6. Review round: dictionary keys are never repeated, whatever their spelling.


def dictionary_fields():
    """Every (model, field) in foundry.models whose type contains a dictionary."""
    from typing import get_args, get_origin

    from pydantic import BaseModel

    from foundry import models

    def has_dict(annotation):
        if annotation is dict or get_origin(annotation) is dict:
            return True
        return any(has_dict(a) for a in get_args(annotation))

    found = set()
    for value in vars(models).values():
        if isinstance(value, type) and issubclass(value, BaseModel) and value.__module__ == models.__name__:
            for name, field in value.model_fields.items():
                if has_dict(field.annotation):
                    found.add((value.__name__, name))
    return found


# Each dictionary field, with a route that accepts the model and a valid body around it.
DICTIONARY_ROUTES = {
    ("RecordCreate", "properties"): (
        "post",
        "/api/businesses/{bid}/records",
        {"kind": "customer", "title": "Synthetic"},
    ),
    ("RecordUpdate", "properties"): (
        "put",
        "/api/businesses/{bid}/records/record-synthetic",
        {"kind": "customer", "title": "Synthetic", "expected_version": 1},
    ),
}


def test_every_dictionary_field_is_covered():
    assert dictionary_fields() == set(DICTIONARY_ROUTES)


def defined_names():
    from pydantic import BaseModel

    from foundry import models

    names = {"body", "query", "path", "header", "cookie", "str", "int", "bool", "constrained-str", "none"}
    for value in vars(models).values():
        if isinstance(value, type) and issubclass(value, BaseModel):
            names.update(value.model_fields)
    return sorted(names)


def locations(detail):
    return [message.split(": ", 1)[0] for message in detail.split("; ")]


@pytest.mark.parametrize("model_field", sorted(DICTIONARY_ROUTES))
@pytest.mark.parametrize("bad", [[1], {"nested": 1}])
def test_dictionary_keys_spelled_like_defined_names_are_not_repeated(client, setup, model_field, bad):
    method, path, body = DICTIONARY_ROUTES[model_field]
    field = model_field[1]
    keys = ["token", "name", "body", "evidence_ids", field, "SYNTHETIC_SECRET_KEY", *defined_names()]
    for key in dict.fromkeys(keys):
        response = client.request(
            method,
            path.format(bid=setup[3]),
            headers={"Idempotency-Key": str(uuid4())},
            json={**body, field: {key: bad}},
        )
        assert response.status_code == 422, key
        detail = response.json()["detail"]
        assert set(locations(detail)) == {"body." + field}, (key, detail)
        # "body" and the field name are the reported location itself; the union member tags
        # are substrings of the fixed type messages ("a valid boolean").
        if key not in {"body", field, "str", "int", "bool", "none"}:
            assert key not in response.text, key
    # Several such keys at once: still only the dictionary field itself.
    response = client.request(
        method,
        path.format(bid=setup[3]),
        headers={"Idempotency-Key": str(uuid4())},
        json={**body, field: {"token": bad, "name": bad, "body": bad, "evidence_ids": bad, field: bad}},
    )
    assert response.status_code == 422
    assert set(locations(response.json()["detail"])) == {"body." + field}
    assert "token" not in response.text and "evidence_ids" not in response.text


def test_defined_locations_stay_useful(client, setup):
    missing = client.post(
        "/api/businesses",
        headers={"Idempotency-Key": str(uuid4())},
        json={"concept": "A valid concept text."},
    )
    assert missing.status_code == 422
    assert missing.json()["detail"] == "body.name: Field required"

    forecast = client.post(
        "/api/businesses/" + setup[3] + "/forecasts",
        headers={"Idempotency-Key": str(uuid4())},
        json={"inputs": {"months": 61}},
    )
    assert forecast.status_code == 422
    assert locations(forecast.json()["detail"]) == ["body.inputs.months"]

    deal = client.post(
        "/api/businesses/" + setup[3] + "/deals",
        headers={"Idempotency-Key": str(uuid4())},
        json={
            "target": "Synthetic target",
            "thesis": "A synthetic acquisition thesis.",
            "adjustments": [{"label": "First", "amount": 1}, {"label": "Second", "amount": "not-a-number"}],
        },
    )
    assert deal.status_code == 422
    assert locations(deal.json()["detail"]) == ["body.adjustments.1.amount"]
    assert "not-a-number" not in deal.text

    extra_item = client.post(
        "/api/businesses/" + setup[3] + "/deals",
        headers={"Idempotency-Key": str(uuid4())},
        json={
            "target": "Synthetic target",
            "thesis": "A synthetic acquisition thesis.",
            "adjustments": [{"label": "First", "amount": 1, "token": 1}],
        },
    )
    assert extra_item.json()["detail"] == "body.adjustments.0: an unexpected field was sent"

    model_rule = client.post(
        "/api/businesses/" + setup[3] + "/forecasts",
        headers={"Idempotency-Key": str(uuid4())},
        json={"inputs": {"discount_rate": "0.05", "terminal_growth": "0.06"}},
    )
    assert model_rule.status_code == 422
    assert locations(model_rule.json()["detail"]) == ["body.inputs"]

    malformed = client.post(
        "/api/businesses",
        headers={"Idempotency-Key": str(uuid4()), "Content-Type": "application/json"},
        content=b'{"name": ',
    )
    assert malformed.status_code == 422
    assert locations(malformed.json()["detail"]) == ["body"]


# 7. Review round: every response carries the security headers.


EXPECTED_HEADERS = {
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "x-frame-options": "DENY",
    "content-security-policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    "cache-control": "no-store",
}


def assert_security_headers(response):
    for name, value in EXPECTED_HEADERS.items():
        assert response.headers.get_list(name) == [value], (name, response.status_code)


def test_early_refusals_and_errors_carry_security_headers(setup):
    with cookie_client(setup) as c:
        cookie_refused = new_business(c, {})
        assert cookie_refused.status_code == 403
        assert_security_headers(cookie_refused)
    app = create_app(setup[0].path, allowed_hosts=["testserver"])
    with TestClient(app, headers={"Authorization": "Bearer " + setup[4]}) as c:
        cross_origin = new_business(c, {"Origin": "https://evil.example"})
        cross_site = new_business(c, {"Sec-Fetch-Site": "cross-site"})
        two_origins = c.post(
            "/api/businesses",
            json={"name": "x", "concept": "A synthetic concept."},
            headers=[("Idempotency-Key", str(uuid4())), ("Origin", ORIGIN), ("Origin", ORIGIN)],
        )
        too_large = c.post(
            "/api/businesses",
            content=b"x" * 2_000_001,
            headers={"Idempotency-Key": str(uuid4()), "Content-Type": "application/json"},
        )
        not_found = c.get("/api/no-such-route")
        invalid = c.post("/api/businesses", headers={"Idempotency-Key": str(uuid4())}, json={})
        unauthorised = c.get("/api/businesses", headers={"Authorization": "Bearer not-a-real-token-value"})
        ok = c.get("/api/health")
        page = c.get("/")
        static = c.get("/static/app.js")
        for response, status in [
            (cross_origin, 403),
            (cross_site, 403),
            (two_origins, 403),
            (too_large, 413),
            (not_found, 404),
            (invalid, 422),
            (unauthorised, 401),
            (ok, 200),
            (page, 200),
            (static, 200),
        ]:
            assert response.status_code == status
            assert_security_headers(response)
    with TestClient(app, base_url="http://evil.example") as wrong_host:
        bad_host = wrong_host.get("/api/health")
        assert bad_host.status_code == 400
        assert_security_headers(bad_host)
        # A write whose Origin matches the wrong host passes the origin check and is refused by
        # the host check; one with a different Origin is refused by the origin check first.
        bad_host_write = wrong_host.post(
            "/api/businesses", json={}, headers={"Origin": "http://evil.example"}
        )
        assert bad_host_write.status_code == 400
        assert_security_headers(bad_host_write)
        bad_host_cross = wrong_host.post(
            "/api/businesses", json={}, headers={"Origin": "https://other.example"}
        )
        assert bad_host_cross.status_code == 403
        assert_security_headers(bad_host_cross)


def test_unhandled_server_error_carries_security_headers_without_detail(setup):
    app = create_app(setup[0].path, allowed_hosts=["testserver"])

    @app.get("/api/synthetic-failure")
    def failure():
        raise RuntimeError("SYNTHETIC-INTERNAL-DETAIL")

    with TestClient(app, raise_server_exceptions=False) as c:
        response = c.get("/api/synthetic-failure")
        assert response.status_code == 500
        assert_security_headers(response)
        assert "SYNTHETIC-INTERNAL-DETAIL" not in response.text
        assert response.json() == {"detail": "The server could not complete this request."}


# 8. Review round: duplicate Origin headers and cookie/authorization combinations.


@pytest.mark.parametrize(
    "order", [(ORIGIN, "https://evil.example"), ("https://evil.example", ORIGIN), (ORIGIN, ORIGIN)]
)
def test_two_origin_headers_are_refused_for_cookie_and_bearer(setup, order):
    before = ai_rows_and_events(setup[0])[1]
    headers = [("Idempotency-Key", str(uuid4())), ("Origin", order[0]), ("Origin", order[1])]
    body = {"name": "Two origins", "concept": "A synthetic duplicate-origin check."}
    with cookie_client(setup) as c:
        response = c.post("/api/businesses", json=body, headers=headers)
        assert response.status_code == 403 and "more than one Origin" in response.json()["detail"]
    app = create_app(setup[0].path, allowed_hosts=["testserver"])
    with TestClient(app, headers={"Authorization": "Bearer " + setup[4]}) as c:
        response = c.post("/api/businesses", json=body, headers=headers)
        assert response.status_code == 403 and "more than one Origin" in response.json()["detail"]
        # Safe methods are not affected.
        assert (
            c.get("/api/businesses", headers=[("Origin", order[0]), ("Origin", order[1])]).status_code == 200
        )
    assert ai_rows_and_events(setup[0])[1] == before


@pytest.mark.parametrize("order", [("same-origin", "cross-site"), ("cross-site", "same-origin")])
def test_two_sec_fetch_site_headers_are_refused(setup, order):
    with cookie_client(setup) as c:
        headers = [
            ("Idempotency-Key", str(uuid4())),
            ("Sec-Fetch-Site", order[0]),
            ("Sec-Fetch-Site", order[1]),
        ]
        response = c.post(
            "/api/businesses", json={"name": "Two sites", "concept": "A synthetic check."}, headers=headers
        )
        assert response.status_code == 403


@pytest.mark.parametrize("authorization", ["", "Basic dXNlcjpwYXNz", "bearer " + "x" * 30, "Bearer"])
def test_cookie_with_non_bearer_authorization_still_needs_origin_evidence(setup, authorization):
    before = ai_rows_and_events(setup[0])[1]
    with cookie_client(setup) as c:
        response = new_business(c, {"Authorization": authorization})
        assert response.status_code == 403
        assert "same-origin" in response.json()["detail"]
    assert ai_rows_and_events(setup[0])[1] == before


def test_cookie_with_lower_case_bearer_of_the_real_key_still_needs_origin_evidence(setup):
    with cookie_client(setup) as c:
        assert new_business(c, {"Authorization": "bearer " + setup[4]}).status_code == 403


def test_cookie_with_invalid_or_revoked_bearer_does_not_fall_back_to_cookie(setup):
    app = create_app(setup[0].path, allowed_hosts=["testserver"])
    with TestClient(app, headers={"Authorization": "Bearer " + setup[4]}) as owner:
        made = owner.post(
            "/api/principals",
            json={"name": "Synthetic operator", "role": "operator", "business_ids": [setup[3]]},
        )
        assert made.status_code == 201
        revoked_token, pid = made.json()["token"], made.json()["id"]
        assert owner.delete("/api/principals/" + pid).status_code == 200
    before = ai_rows_and_events(setup[0])[1]
    with cookie_client(setup) as c:
        for token in ["not-a-real-token-value-xxxxxxxx", revoked_token]:
            response = new_business(c, {"Authorization": "Bearer " + token})
            assert response.status_code == 401, token
            # With origin evidence as well, the bearer credential still decides.
            assert new_business(c, {"Authorization": "Bearer " + token, "Origin": ORIGIN}).status_code == 401
            assert c.get("/api/businesses", headers={"Authorization": "Bearer " + token}).status_code == 401
        # The cookie alone still works for reads, so the 401 was the bearer's, not the session's.
        assert c.get("/api/businesses").status_code == 200
    assert ai_rows_and_events(setup[0])[1] == before


def test_public_origin_setting_decides_the_expected_origin(setup, monkeypatch):
    monkeypatch.setenv("FOUNDRY_PUBLIC_ORIGIN", "https://foundry.example")
    with cookie_client(setup) as c:
        before = ai_rows_and_events(setup[0])[1]
        assert new_business(c, {"Origin": ORIGIN}).status_code == 403
        assert new_business(c, {"Origin": "https://evil.example"}).status_code == 403
        assert ai_rows_and_events(setup[0])[1] == before
        assert new_business(c, {"Origin": "https://foundry.example"}).status_code == 201
        assert ai_rows_and_events(setup[0])[1] > before


def test_refused_cookie_writes_store_no_event(setup):
    before = ai_rows_and_events(setup[0])[1]
    with cookie_client(setup) as c:
        for headers in [
            {},
            {"Sec-Fetch-Site": "same-site"},
            {"Origin": "https://evil.example"},
            {"Origin": ORIGIN, "Sec-Fetch-Site": "cross-site"},
        ]:
            assert new_business(c, headers).status_code == 403
        assert c.post("/api/logout").status_code == 403
    assert ai_rows_and_events(setup[0])[1] == before
    with setup[0].read() as con:
        assert con.execute("SELECT COUNT(*) FROM businesses").fetchone()[0] == 1
