from __future__ import annotations

import os
import time
from collections import defaultdict, deque
from pathlib import Path
from types import UnionType
from typing import Annotated, Union, get_args, get_origin
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.datastructures import MutableHeaders
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import catalog, models
from .ai import Adviser
from .service import Service
from .store import DomainError, Store, digest

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
    "Cache-Control": "no-store",
}


class SecurityHeaders:
    """Add the security headers to every HTTP response that passes through the application.

    A pure ASGI wrapper placed outside every other application middleware, so early refusals
    (host, origin, size) carry the headers as well as routed responses.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_headers)


def _unwrap(annotation):
    """Strip Annotated metadata and Optional; a union of several real types is left as is."""
    while True:
        origin = get_origin(annotation)
        if origin is Annotated:
            annotation = get_args(annotation)[0]
        elif origin in (Union, UnionType):
            members = [a for a in get_args(annotation) if a is not type(None)]
            if len(members) != 1:
                return annotation
            annotation = members[0]
        else:
            return annotation


def _model_field(model, part):
    if not isinstance(part, str):
        return None
    for name, field in model.model_fields.items():
        if part == name or (field.alias and part == field.alias):
            return field
    return None


def defined_path(annotation, parts):
    """Keep the leading location parts that the declared type itself defines.

    Model field names and list positions are kept. The walk stops at anything else: a
    dictionary (its keys are chosen by the client, whatever their spelling), a union member
    tag, a scalar, or a name the model does not declare. The decision is made from the type
    structure, never from the spelling of the client's input.
    """
    kept = []
    for part in parts:
        annotation = _unwrap(annotation)
        origin = get_origin(annotation)
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            field = _model_field(annotation, part)
            if field is None:
                break
            kept.append(part)
            annotation = field.annotation
        elif origin in (list, tuple, set, frozenset) and isinstance(part, int) and not isinstance(part, bool):
            args = [a for a in get_args(annotation) if a is not Ellipsis]
            if origin is tuple and len(args) != 1:
                break
            kept.append(str(part))
            annotation = args[0] if args else object
        else:
            break
    return kept


def _route_params(dependant, kind):
    params = list(getattr(dependant, kind))
    for sub in dependant.dependencies:
        params.extend(_route_params(sub, kind))
    return params


def defined_location(route, loc):
    """The application-defined part of a validation error location, as a list of strings."""
    if not loc or loc[0] not in {"body", "query", "path", "header", "cookie"}:
        return []
    root, rest = loc[0], list(loc[1:])
    dependant = getattr(route, "dependant", None)
    if dependant is None:
        return [root]
    params = _route_params(dependant, root + "_params")
    if root == "body" and len(params) == 1 and not getattr(params[0].field_info, "embed", False):
        # A single, non-embedded body model: the location after "body" is the model's own path.
        return [root, *defined_path(params[0].field_info.annotation, rest)]
    if not rest:
        return [root]
    for param in params:
        if rest[0] == param.alias:
            return [root, param.alias, *defined_path(param.field_info.annotation, rest[1:])]
    return [root]


def validation_message(error, route=None):
    """Describe a validation error by locations the application defines, never by client input.

    The location is cut where the route's declared request types stop defining it: at a
    client-chosen dictionary key (whatever its spelling), an unexpected property, or a union
    member tag. Model field names and list positions are kept.
    """
    loc = list(error.get("loc", ()))
    extra = error.get("type") == "extra_forbidden"
    if extra:
        loc = loc[:-1]
    where = ".".join(defined_location(route, loc)) or "request"
    return where + ": " + ("an unexpected field was sent" if extra else error["msg"])


def create_app(db_path=None, *, allowed_hosts=None, ai_transport=None, desktop=None):
    """Build the web application.

    ``desktop`` is the desktop app's launch sign-in (``foundry.desktop.LaunchSignIn``). When it
    is given, only its exact ``host`` (127.0.0.1 and the port) is accepted, writes are judged
    against its exact ``origin`` (``FOUNDRY_PUBLIC_ORIGIN`` is ignored), the window signs in at
    ``/desktop/start`` with the launch code, and a page opened without that session gets a plain
    instruction page instead of the sign-in form. An owner's ``/api/me`` then also holds
    ``desktop``: the path of the database this server opened and the command that backs it up
    (``foundry.desktop.backup_command``). No other role, and no ordinary server, gets that field.
    """
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
    desktop_database = None
    if desktop is not None:
        from .desktop import backup_command

        # The database this server opened, told to the window's owner so the settings page can
        # name it in the backup command.
        database = str(store.path)
        desktop_database = {"database": database, "backup_command": backup_command(database, os.name)}

    @app.exception_handler(DomainError)
    async def domain_error(request, exc):
        return JSONResponse({"detail": exc.message}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Do not echo submitted credentials, evidence text, or other request bodies into errors.
        # Values are never included, and neither are names the client invented.
        messages = []
        for error in exc.errors():
            message = validation_message(error, request.scope.get("route"))
            if message not in messages:
                messages.append(message)
        return JSONResponse({"detail": "; ".join(messages)}, status_code=422)

    @app.middleware("http")
    async def boundaries(request: Request, call_next):
        # The host middleware ignores the port. The desktop server accepts only its own exact
        # 127.0.0.1:<port>, so a web page reaching it through a rebound DNS name is refused.
        if desktop is not None and request.headers.getlist("host") != [desktop.host]:
            return PlainTextResponse("Invalid host header", status_code=400)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            # Two Origin (or Sec-Fetch-Site) headers are ambiguous: a browser never sends them,
            # and judging by whichever comes first would let one value hide the other.
            if (
                len(request.headers.getlist("origin")) > 1
                or len(request.headers.getlist("sec-fetch-site")) > 1
            ):
                return JSONResponse(
                    {"detail": "Writes with more than one Origin or Sec-Fetch-Site header are not accepted."},
                    status_code=403,
                )
            origin = request.headers.get("origin")
            if desktop is not None:
                # The window's origin is known exactly. FOUNDRY_PUBLIC_ORIGIN describes a server
                # deployment; left over in the environment, it would silently refuse every save.
                expected = desktop.origin
            else:
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
        return await call_next(request)

    # Added last, so it wraps the boundary checks and the host check: their refusals carry the
    # security headers too.
    app.add_middleware(SecurityHeaders)

    @app.exception_handler(Exception)
    async def server_error(request, exc):
        # Starlette sends unhandled errors from its outermost error middleware, outside
        # SecurityHeaders, so this response sets the headers itself. The exception is still
        # re-raised to the server for logging; no exception text reaches the client.
        return JSONResponse(
            {"detail": "The server could not complete this request."},
            status_code=500,
            headers=SECURITY_HEADERS,
        )

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
        answer = {**who, "ai_configured": adviser.configured()}
        if desktop_database is not None and who["role"] == "owner":
            answer["desktop"] = dict(desktop_database)
        return answer

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
        return service.snapshot(who, bid, guide=True)

    @app.get("/api/businesses/{bid}/guide")
    def guide(bid: str, who=Depends(actor)):
        """Where to start, for the person asking: read-only, worked out from the current records."""
        return service.guide(who, bid)

    @app.put("/api/businesses/{bid}")
    def update_business(bid: str, body: models.BusinessUpdate, who=Depends(actor), command=Depends(key)):
        return service.update_business(who, bid, command, body.model_dump(mode="json"))

    @app.get("/api/businesses/{bid}/jurisdiction/preview")
    def preview_jurisdiction(bid: str, to: models.Jurisdiction, who=Depends(actor)):
        return service.preview_jurisdiction(who, bid, to)

    @app.post("/api/businesses/{bid}/jurisdiction")
    def change_jurisdiction(
        bid: str, body: models.JurisdictionChange, who=Depends(actor), command=Depends(key)
    ):
        return service.change_jurisdiction(who, bid, command, body.model_dump(mode="json"))

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
        """Screen an acquisition and create its diligence steps.

        annual_revenue, reported_ebitda and purchase_price are required. Every other figure left out
        takes the model's example value, and the deal lists it in data.assumed_inputs, in field order.
        """
        # Only the fields the client sent: the service records every figure left out as assumed.
        return service.save_deal(who, bid, command, body.model_dump(mode="json", exclude_unset=True))

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

    if desktop is not None:
        from .desktop import BROWSER_PAGE

        def signed_in(request):
            try:
                store.authenticate(request.cookies.get("foundry_session", ""), session=True)
                return True
            except DomainError:
                return False

        @app.get("/desktop/start", include_in_schema=False)
        def desktop_start(code: str = ""):
            token = desktop.redeem(code)
            if not token:
                return HTMLResponse(BROWSER_PAGE, status_code=403)
            # Redirect so the code leaves the address. No max_age: the cookie ends with the window.
            response = RedirectResponse("/", status_code=303)
            response.set_cookie("foundry_session", token, httponly=True, samesite="strict")
            return response

        @app.get("/")
        def desktop_index(request: Request):
            if not signed_in(request):
                return HTMLResponse(BROWSER_PAGE, status_code=403)
            return FileResponse(static / "index.html")

    else:

        @app.get("/")
        def index():
            return FileResponse(static / "index.html")

    return app
