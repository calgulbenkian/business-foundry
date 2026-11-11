from __future__ import annotations

import os
import time
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import catalog, models
from .ai import Adviser
from .service import Service
from .store import DomainError, Store, digest

LOCATION_ROOTS = {"body", "query", "path", "header", "cookie"}


def defined_names():
    names = set(LOCATION_ROOTS)
    for value in vars(models).values():
        if isinstance(value, type) and issubclass(value, BaseModel):
            for name, field in value.model_fields.items():
                names.add(name)
                if field.alias:
                    names.add(field.alias)
    return names


DEFINED_NAMES = defined_names()


def validation_message(error):
    """Describe a validation error by locations the application defines, never by client input.

    The location is cut at the first segment that is not an application-defined field name
    (for example a client-chosen dictionary key or an unexpected property), so client-invented
    names are never echoed. List positions are kept.
    """
    loc = list(error.get("loc", ()))
    extra = error.get("type") == "extra_forbidden"
    if extra:
        loc = loc[:-1]
    kept = []
    for part in loc:
        if isinstance(part, int) or (isinstance(part, str) and part in DEFINED_NAMES):
            kept.append(str(part))
        else:
            break
    where = ".".join(kept) or "request"
    return where + ": " + ("an unexpected field was sent" if extra else error["msg"])


def create_app(db_path=None, *, allowed_hosts=None, ai_transport=None):
    store = Store(db_path or os.getenv("FOUNDRY_DB", ".foundry/foundry.db"))
    service, app = (
        Service(store),
        FastAPI(
            title="Business Foundry",
            version="0.1.0",
            # The Swagger page needs third-party scripts and an inline script, which the
            # content security policy forbids; publish the schema only.
            docs_url=None,
            openapi_url="/api/openapi.json",
            redoc_url=None,
        ),
    )
    adviser = Adviser(service, ai_transport)
    app.state.store, app.state.service, app.state.adviser = store, service, adviser
    hosts = allowed_hosts or os.getenv("FOUNDRY_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]").split(",")
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)
    failed_logins = defaultdict(deque)

    @app.exception_handler(DomainError)
    async def domain_error(request, exc):
        return JSONResponse({"detail": exc.message}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Do not echo submitted credentials, evidence text, or other request bodies into errors.
        # Values are never included, and neither are names the client invented.
        messages = []
        for error in exc.errors():
            message = validation_message(error)
            if message not in messages:
                messages.append(message)
        return JSONResponse({"detail": "; ".join(messages)}, status_code=422)

    @app.middleware("http")
    async def boundaries(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            expected = os.getenv("FOUNDRY_PUBLIC_ORIGIN", str(request.base_url).rstrip("/"))
            if origin and origin != expected:
                return JSONResponse({"detail": "Cross-origin writes are not accepted."}, status_code=403)
            fetch_site = request.headers.get("sec-fetch-site")
            if fetch_site == "cross-site":
                return JSONResponse({"detail": "Cross-site writes are not accepted."}, status_code=403)
            # A cookie is sent by the browser automatically, so a cookie-authenticated write must
            # carry affirmative same-origin evidence. Bearer callers present their credential
            # deliberately; sign-in without a session has no cookie to abuse.
            bearer = request.headers.get("authorization", "").startswith("Bearer ")
            if (
                "foundry_session" in request.cookies
                and not bearer
                and origin != expected
                and fetch_site != "same-origin"
            ):
                return JSONResponse(
                    {
                        "detail": "Browser-session writes must come from this application's own "
                        "pages (same-origin Origin or Sec-Fetch-Site header). API callers should "
                        "use Authorization: Bearer."
                    },
                    status_code=403,
                )
            size = 0
            chunks = []
            async for chunk in request.stream():
                size += len(chunk)
                if size > 2_000_000:
                    return JSONResponse({"detail": "Request exceeds the 2 MB limit."}, status_code=413)
                chunks.append(chunk)
            request._body = b"".join(chunks)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    def actor(request: Request):
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            return store.authenticate(auth[7:])
        return store.authenticate(request.cookies.get("foundry_session", ""), session=True)

    def key(idempotency_key: str = Header(default="")):
        if not idempotency_key or len(idempotency_key) > 200:
            raise DomainError("An Idempotency-Key of 1-200 characters is required.", 400)
        return idempotency_key

    @app.get("/api/health")
    def health():
        return {"status": "ok", "version": "0.1.0"}

    @app.post("/api/login")
    def login(body: models.Login, request: Request):
        address = request.client.host if request.client else "local"
        queue = failed_logins[address]
        cutoff = time.monotonic() - 60
        while queue and queue[0] < cutoff:
            queue.popleft()
        if len(queue) >= 10:
            raise DomainError("Too many sign-in attempts. Wait a minute.", 429)
        try:
            who = store.authenticate(body.token)
        except DomainError:
            queue.append(time.monotonic())
            raise
        queue.clear()
        result = JSONResponse(who)
        result.set_cookie(
            "foundry_session",
            store.session(who),
            httponly=True,
            samesite="strict",
            secure=urlsplit(str(request.url)).scheme == "https",
            max_age=8 * 3600,
        )
        return result

    @app.post("/api/logout")
    def logout(request: Request, who=Depends(actor)):
        with store.write() as con:
            con.execute(
                "DELETE FROM sessions WHERE token_hash=?",
                (digest(request.cookies.get("foundry_session", "")),),
            )
        response = JSONResponse({"signed_out": True})
        response.delete_cookie("foundry_session")
        return response

    @app.get("/api/me")
    def me(who=Depends(actor)):
        return {**who, "ai_configured": adviser.configured()}

    @app.get("/api/catalog")
    def get_catalog(who=Depends(actor)):
        return catalog.description()

    @app.get("/api/businesses")
    def businesses(who=Depends(actor)):
        return service.list_businesses(who)

    @app.post("/api/businesses", status_code=201)
    def create_business(body: models.BusinessCreate, who=Depends(actor), command=Depends(key)):
        return service.create_business(who, command, body.model_dump(mode="json"))

    @app.get("/api/businesses/{bid}")
    def snapshot(bid: str, who=Depends(actor)):
        return service.snapshot(who, bid)

    @app.put("/api/businesses/{bid}")
    def update_business(bid: str, body: models.BusinessUpdate, who=Depends(actor), command=Depends(key)):
        return service.update_business(who, bid, command, body.model_dump(mode="json"))

    @app.post("/api/businesses/{bid}/records", status_code=201)
    def create_record(bid: str, body: models.RecordCreate, who=Depends(actor), command=Depends(key)):
        return service.add_record(who, bid, command, body.model_dump(mode="json"))

    @app.put("/api/businesses/{bid}/records/{oid}")
    def update_record(
        bid: str, oid: str, body: models.RecordUpdate, who=Depends(actor), command=Depends(key)
    ):
        return service.add_record(who, bid, command, body.model_dump(mode="json"), oid)

    @app.post("/api/businesses/{bid}/evidence", status_code=201)
    def evidence(bid: str, body: models.EvidenceCreate, who=Depends(actor), command=Depends(key)):
        return service.add_evidence(who, bid, command, body.model_dump(mode="json"))

    @app.post("/api/businesses/{bid}/evidence/{eid}/review")
    def review_evidence(bid: str, eid: str, body: models.Review, who=Depends(actor), command=Depends(key)):
        return service.review_evidence(who, bid, eid, command, body.model_dump(mode="json"))

    @app.post("/api/businesses/{bid}/evidence/{eid}/revoke")
    def revoke_evidence(bid: str, eid: str, body: models.Review, who=Depends(actor), command=Depends(key)):
        return service.review_evidence(who, bid, eid, command, body.model_dump(mode="json"), True)

    @app.post("/api/businesses/{bid}/tasks/{tid}/transition")
    def transition(bid: str, tid: str, body: models.TaskTransition, who=Depends(actor), command=Depends(key)):
        return service.transition(who, bid, tid, command, body.model_dump(mode="json"))

    @app.post("/api/businesses/{bid}/forecasts", status_code=201)
    def forecast(bid: str, body: models.ForecastCreate, who=Depends(actor), command=Depends(key)):
        return service.save_forecast(who, bid, command, body.model_dump(mode="json"))

    @app.post("/api/businesses/{bid}/deals", status_code=201)
    def deal(bid: str, body: models.DealInputs, who=Depends(actor), command=Depends(key)):
        return service.save_deal(who, bid, command, body.model_dump(mode="json"))

    @app.post("/api/businesses/{bid}/documents", status_code=201)
    def document(bid: str, body: models.DocumentCreate, who=Depends(actor), command=Depends(key)):
        return service.generate_document(who, bid, command, body.model_dump(mode="json"))

    @app.get("/api/businesses/{bid}/documents/{did}/download")
    def download_document(bid: str, did: str, who=Depends(actor)):
        service.access(who, bid)
        with store.read() as con:
            doc = store.get(con, bid, did, "document")
        return PlainTextResponse(
            doc["data"]["content"],
            media_type="text/markdown",
            headers={"Content-Disposition": f'attachment; filename="{doc["id"]}.md"'},
        )

    @app.post("/api/businesses/{bid}/invoices", status_code=201)
    def invoice(bid: str, body: models.InvoiceCreate, who=Depends(actor), command=Depends(key)):
        return service.create_invoice(who, bid, command, body.model_dump(mode="json"))

    @app.post("/api/businesses/{bid}/invoices/{iid}/issue")
    def issue(bid: str, iid: str, body: models.InvoiceIssue, who=Depends(actor), command=Depends(key)):
        return service.issue_invoice(who, bid, iid, command, body.model_dump(mode="json"))

    @app.post("/api/businesses/{bid}/invoices/{iid}/payments", status_code=201)
    def payment(bid: str, iid: str, body: models.PaymentCreate, who=Depends(actor), command=Depends(key)):
        return service.payment(who, bid, iid, command, body.model_dump(mode="json"))

    @app.get("/api/businesses/{bid}/audit")
    def audit(bid: str, who=Depends(actor)):
        return service.audit(who, bid)

    @app.get("/api/businesses/{bid}/export")
    def export(bid: str, who=Depends(actor)):
        return JSONResponse(
            service.audit(who, bid, True),
            headers={"Content-Disposition": 'attachment; filename="business-foundry-export.json"'},
        )

    @app.get("/api/principals")
    def principals(who=Depends(actor)):
        return service.principals(who)

    @app.post("/api/principals", status_code=201)
    def principal(body: models.PrincipalCreate, who=Depends(actor)):
        # Raw credentials are returned once and never stored in the command-response cache.
        return service.add_principal(who, body.model_dump(mode="json"))

    @app.delete("/api/principals/{pid}")
    def revoke(pid: str, who=Depends(actor)):
        return service.revoke_principal(who, pid)

    @app.get("/api/businesses/{bid}/ai")
    def ai_runs(bid: str, who=Depends(actor)):
        return adviser.list_runs(who, bid)

    @app.post("/api/businesses/{bid}/ai")
    async def ai(bid: str, body: models.AIRequest, who=Depends(actor), command=Depends(key)):
        return await adviser.review(who, bid, command, body.model_dump(mode="json"))

    static = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/")
    def index():
        return FileResponse(static / "index.html")

    return app
