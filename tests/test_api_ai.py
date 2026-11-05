import json
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient

from foundry.api import create_app


def test_api_auth_session_csrf_and_security_headers(setup):
    store, _, _, bid, token = setup
    app = create_app(store.path, allowed_hosts=["testserver"])
    with TestClient(app) as c:
        assert c.get("/api/businesses").status_code == 401
        response = c.post("/api/login", json={"token": token})
        assert response.status_code == 200
        cookie = response.headers["set-cookie"]
        assert "HttpOnly" in cookie and "SameSite=strict" in cookie
        assert c.get("/api/businesses/" + bid).status_code == 200
        response = c.post(
            "/api/businesses",
            json={"name": "No", "concept": "Blocked cross origin"},
            headers={"Origin": "https://evil.example", "Idempotency-Key": "bad"},
        )
        assert response.status_code == 403
        assert c.get("/").headers["content-security-policy"].startswith("default-src 'self'")
        c.post("/api/logout")
        assert c.get("/api/businesses").status_code == 401
        assert c.get("/api/health", headers={"Host": "evil.example"}).status_code == 400


def test_api_body_limits_errors_do_not_echo_input_and_missing_key(client, setup):
    bid = setup[3]
    assert (
        client.post(
            "/api/businesses/" + bid + "/records", json={"kind": "customer", "title": "No key"}
        ).status_code
        == 400
    )
    secret = "super-private-sentinel"
    response = client.post(
        "/api/businesses",
        headers={"Idempotency-Key": "new"},
        json={"name": "Bad", "concept": secret, "unexpected": secret},
    )
    assert response.status_code == 422 and secret not in response.text
    assert client.post("/api/login", content="x" * 2_000_001).status_code == 413


def test_api_full_write_read_document_export(client, setup):
    bid = setup[3]
    r = client.post(
        "/api/businesses/" + bid + "/records",
        headers={"Idempotency-Key": "record"},
        json={"kind": "customer", "title": "A real test record", "details": "A buyer with a stated problem."},
    )
    assert r.status_code == 201
    doc = client.post(
        "/api/businesses/" + bid + "/documents",
        headers={"Idempotency-Key": "doc"},
        json={"template": "business_plan"},
    )
    assert doc.status_code == 201
    download = client.get("/api/businesses/" + bid + "/documents/" + doc.json()["id"] + "/download")
    assert download.status_code == 200 and "A real test record" in download.text
    assert "DRAFT" in download.text and "Unknown" in download.text
    assert client.get("/api/businesses/" + bid + "/export").json()["integrity"]["valid"]


def ai_client(setup, monkeypatch, handler):
    monkeypatch.setenv("OPENAI_API_KEY", "test-not-a-real-key")
    monkeypatch.setenv("FOUNDRY_OPENAI_MODEL", "example-model")
    return TestClient(
        create_app(setup[0].path, allowed_hosts=["testserver"], ai_transport=httpx.MockTransport(handler)),
        headers={"Authorization": "Bearer " + setup[4]},
    )


def ai_request(c, bid, key="ai-1"):
    return c.post(
        "/api/businesses/" + bid + "/ai",
        headers={"Idempotency-Key": key},
        json={
            "question": "Which assumptions should we test first?",
            "share_with_provider": True,
            "max_output_tokens": 1000,
        },
    )


def test_ai_has_no_tools_records_provenance_and_retries_do_not_rebill(setup, monkeypatch):
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        assert body["store"] is False and "tools" not in body
        assert "test-not-a-real-key" not in request.content.decode()
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(
                                    {
                                        "summary": "Test demand first.",
                                        "recommendations": [
                                            {
                                                "action": "Interview a buyer",
                                                "reason": "Demand is unknown.",
                                                "evidence_ids": [],
                                            }
                                        ],
                                        "unknowns": ["Willingness to pay"],
                                    }
                                ),
                            }
                        ]
                    }
                ],
                "usage": {"input_tokens": 10, "output_tokens": 20},
            },
        )

    with ai_client(setup, monkeypatch, handler) as c:
        first = ai_request(c, setup[3])
        second = ai_request(c, setup[3])
        assert first.status_code == 200 and first.json()["state"] == "succeeded"
        assert second.json() == first.json() and len(calls) == 1
        assert first.json()["data"]["usage"]["output_tokens"] == 20
        assert c.get("/api/businesses/" + setup[3] + "/audit").json()["integrity"]["valid"]
        assert len(c.get("/api/businesses/" + setup[3]).json()["objects"]) == 17


def test_ai_unknown_citation_rejected_and_daily_limit_enforced(setup, monkeypatch):
    monkeypatch.setenv("FOUNDRY_AI_DAILY_CALLS", "1")

    def handler(request):
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(
                                    {
                                        "summary": "Bad citation",
                                        "recommendations": [
                                            {
                                                "action": "Act",
                                                "reason": "Source",
                                                "evidence_ids": ["invented-id"],
                                            }
                                        ],
                                        "unknowns": [],
                                    }
                                ),
                            }
                        ]
                    }
                ],
            },
        )

    with ai_client(setup, monkeypatch, handler) as c:
        assert ai_request(c, setup[3]).json()["state"] == "failed"
        assert ai_request(c, setup[3], "new-key").status_code == 429


def test_ai_timeout_is_unknown_not_retried(setup, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("unconfirmed", request=request)

    with ai_client(setup, monkeypatch, handler) as c:
        assert ai_request(c, setup[3]).json()["state"] == "unknown"
        assert ai_request(c, setup[3]).json()["state"] == "unknown"
        assert len(calls) == 1


def test_ai_requires_explicit_sharing_and_configuration(client, setup, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert ai_request(client, setup[3]).status_code == 503
    response = client.post(
        "/api/businesses/" + setup[3] + "/ai",
        headers={"Idempotency-Key": str(uuid4())},
        json={"question": "What should we test first?", "share_with_provider": False},
    )
    assert response.status_code == 422
