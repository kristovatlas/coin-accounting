"""The FastAPI app (architecture §1, §4; THREAT_MODEL T-101 to T-110).

`create_app` wires the security layer around the routes. The launcher passes in what the app needs
as values and callbacks: the port, the bootstrap token, the node status, and what to do on claim
and on quit. The API itself opens no files and starts nothing.

Routes:
- `GET /` and `GET /assets/<file>`: the built frontend, handed in by the launcher, which reads it
  (`api/` opens no files). Without a build, `GET /` and `GET /app.js` serve the placeholder page.
  No authentication; no data.
- `POST /api/session`: bootstrap token → session token, once (§4).
- `GET /api/status` (bearer): the node status from start-up.
- `POST /api/quit` (bearer): start shutdown.
- Accounts and imports (bearer; PLAN §3, T-701, T-703), through `services.imports.Imports`:
  - `GET /api/accounts`: the entities, tax accounts and wallet clients.
  - `POST /api/entities`, `/api/tax-accounts`, `/api/clients`: add one.
  - `POST /api/imports/addresses/preview` and `/api/imports/descriptor/preview`: what an upload holds.
  - `POST /api/imports/addresses` and `/api/imports/descriptor`: import it (the UI sends this only
    after the user confirmed the preview of the same upload).

  A refusal is a 422 with the service's fixed message, which never repeats the upload (T-403,
  T-703); a busy DB is a 503; a descriptor while offline is a 409 (T-203). Upload size is bounded by
  the security layer's body limit, before any of this runs.
- History (bearer; PLAN §3), through `services.history.History`, from the chain cache only, as of the
  last finished sync (`as_of`; each address's `scanned_to` says how far its own history is scanned):
  - `GET /api/addresses[?tax_account_id=N]`: every address, with its owner, balance, UTXO count and
    activity.
  - `POST /api/addresses/events` `{"script": …}`: one address's receives and spends (404 if not in
    the DB; 422 for a script that isn't lowercase hex). A POST, so the script never appears in a URL
    (T-105).
  - `GET /api/utxos[?tax_account_id=N]`: the user's own unspent outputs, oldest first, each marked
    `complete` when its address's history is scanned to `as_of`.

  A busy DB is a 503, and an unusable one a 422 with a fixed message, as for the import routes.

FastAPI's docs, OpenAPI schema and default validation errors are off: the docs page loads
third-party assets (T-106), and the default error body echoes the request input.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from coinacct.api.placeholder import APP_JS, INDEX_HTML
from coinacct.api.security import MAX_BODY_BYTES, SecurityMiddleware
from coinacct.api.session import ClaimError, Sessions
from coinacct.domain.keys import PrivateKeyError
from coinacct.services import imports as import_service
from coinacct.services.history import History
from coinacct.services.imports import Busy, ImportRefused, Imports, OfflineError, Owner
from coinacct.services.startup import NodeStatus

log = logging.getLogger(__name__)

# The file types a Vite build emits; anything else in the bundle is not served.
BUNDLE_TYPES = {
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".woff2": "font/woff2",
}

CLAIM_STATUS = {ClaimError.WRONG_TOKEN: 401, ClaimError.EXPIRED: 410, ClaimError.ALREADY_CLAIMED: 409}


class SessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bootstrap: str = Field(min_length=1, max_length=200)


class Unauthorized(Exception):
    pass


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NewEntity(Strict):
    name: str = Field(min_length=1, max_length=200)
    kind: Literal["exchange", "employer", "merchant", "person", "unknown"]


class NewTaxAccount(Strict):
    name: str = Field(min_length=1, max_length=200)
    kind: Literal["self_custody", "custodial"]
    entity_id: int | None = None


class NewClient(Strict):
    name: str = Field(min_length=1, max_length=200)
    kind: Literal["hardware", "mobile", "desktop", "web", "paper", "other"]


class Upload(Strict):
    # The security layer's body limit comes first (a 413 above it), so it is the real upload limit:
    # a longer list is imported in parts.
    text: str = Field(max_length=MAX_BODY_BYTES)


class DescriptorUpload(Upload):
    gap_limit: int = Field(default=import_service.DEFAULT_GAP_LIMIT, ge=1, le=import_service.MAX_GAP_LIMIT)


class Owned(Strict):
    entity_id: int
    tax_account_id: int | None
    label: str = Field(default="", max_length=200)
    start_height: int = Field(default=0, ge=0)
    client_ids: list[int] = Field(default_factory=list, max_length=50)

    def owner(self) -> Owner:
        return Owner(
            self.entity_id, self.tax_account_id, self.label, self.start_height, tuple(self.client_ids)
        )


class OwnedUpload(Upload, Owned):
    pass


class OwnedDescriptorUpload(DescriptorUpload, Owned):
    pass


def _known(known: Any) -> list[dict[str, Any]]:
    return [
        {
            "script": k.script_hex,
            "address": k.address,
            "entity_id": k.entity_id,
            "tax_account_id": k.tax_account_id,
        }
        for k in known
    ]


def create_app(  # noqa: PLR0913 - each is a value or callback the launcher hands in
    *,
    port: int,
    sessions: Sessions,
    status: Callable[[], NodeStatus],
    on_quit: Callable[[], None],
    bundle: Mapping[str, bytes] | None = None,
    imports: Imports | None = None,
    history: History | None = None,
) -> SecurityMiddleware:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    def require_session(authorization: Annotated[str | None, Header()] = None) -> None:
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not sessions.is_session(token.strip()):
            raise Unauthorized

    authenticated = [Depends(require_session)]

    @app.exception_handler(Unauthorized)
    async def unauthorized(request: Request, exc: Unauthorized) -> Response:
        return JSONResponse(
            {"error": "not authenticated"}, status_code=401, headers={"WWW-Authenticate": "Bearer"}
        )

    @app.exception_handler(RequestValidationError)
    async def invalid(request: Request, exc: RequestValidationError) -> Response:
        return JSONResponse({"error": "invalid request"}, status_code=422)

    # The services' refusals carry fixed messages that never repeat the input (T-403, T-703).
    @app.exception_handler(OfflineError)
    async def offline(request: Request, exc: OfflineError) -> Response:
        return JSONResponse({"error": str(exc)}, status_code=409)

    @app.exception_handler(ImportRefused)
    @app.exception_handler(PrivateKeyError)
    async def refused(request: Request, exc: Exception) -> Response:
        return JSONResponse({"error": str(exc)}, status_code=422)

    @app.exception_handler(Busy)
    async def busy(request: Request, exc: Busy) -> Response:
        return JSONResponse({"error": str(exc)}, status_code=503)

    if bundle is not None:
        index_html = bundle["index.html"]

        @app.get("/")
        def index() -> Response:
            return Response(index_html, media_type="text/html; charset=utf-8")

        @app.get("/assets/{name}")
        def asset(name: str) -> Response:
            body = bundle.get(f"assets/{name}")
            media_type = BUNDLE_TYPES.get(name[name.rfind(".") :] if "." in name else "")
            if body is None or media_type is None:
                return JSONResponse({"error": "not found"}, status_code=404)
            return Response(body, media_type=media_type)

    else:

        @app.get("/")
        def placeholder() -> Response:
            return Response(INDEX_HTML, media_type="text/html; charset=utf-8")

        @app.get("/app.js")
        def script() -> Response:
            return Response(APP_JS, media_type="text/javascript; charset=utf-8")

    @app.post("/api/session")
    def claim(body: SessionRequest) -> Response:
        result = sessions.claim(body.bootstrap)
        if isinstance(result, ClaimError):
            return JSONResponse({"error": result.value}, status_code=CLAIM_STATUS[result])
        return JSONResponse({"session": result})

    @app.get("/api/status", dependencies=authenticated)
    def node_status() -> dict[str, Any]:
        current = status()
        return {"online": current.online, "chain": current.chain, "reasons": list(current.reasons)}

    @app.post("/api/quit", status_code=202, dependencies=authenticated)
    def quit_app() -> dict[str, str]:
        log.info("quit requested from the UI")
        on_quit()
        return {"result": "shutting down"}

    if imports is not None:
        _import_routes(app, imports, authenticated)
    if history is not None:
        _history_routes(app, history, authenticated)
    return SecurityMiddleware(app, port=port)


class ScriptQuery(Strict):
    script: str = Field(pattern=r"^(?:[0-9a-f]{2}){1,10000}$")  # a script, as lowercase hex


def _tip(tip: Any) -> dict[str, Any] | None:
    return None if tip is None else {"blockhash": tip.blockhash, "height": tip.height}


def _history_routes(app: FastAPI, history: History, authenticated: list[Any]) -> None:
    @app.get("/api/addresses", dependencies=authenticated)
    def addresses(tax_account_id: Annotated[int | None, Query()] = None) -> dict[str, Any]:
        found = history.addresses(tax_account_id)
        return {
            "as_of": _tip(found.as_of),
            "catching_up": found.catching_up,
            "addresses": [
                {
                    "script": a.script_hex,
                    "address": a.address,
                    "entity_id": a.entity_id,
                    "tax_account_id": a.tax_account_id,
                    "label": a.label,
                    "balance": a.balance,
                    "utxos": a.utxos,
                    "received": a.received,
                    "transactions": a.transactions,
                    "last_height": a.last_height,
                    "scanned_to": a.scanned_to,
                }
                for a in found.addresses
            ],
        }

    # POST, with the script in the body: a script in the URL could be kept by the browser, outside the
    # volume (T-105: sensitive queries use request bodies).
    @app.post("/api/addresses/events", dependencies=authenticated)
    def events(body: ScriptQuery) -> Response:
        found = history.events(body.script)
        if found is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse(
            {
                "events": [
                    {
                        "kind": e.kind,
                        "txid": e.txid,
                        "n": e.n,
                        "sats": e.sats,
                        "height": e.height,
                        "blockhash": e.blockhash,
                        "prevout": None
                        if e.prevout is None
                        else {"txid": e.prevout.txid, "vout": e.prevout.vout},
                    }
                    for e in found
                ]
            }
        )

    @app.get("/api/utxos", dependencies=authenticated)
    def utxos(tax_account_id: Annotated[int | None, Query()] = None) -> dict[str, Any]:
        return {
            "utxos": [
                {
                    "txid": u.txid,
                    "vout": u.vout,
                    "sats": u.sats,
                    "script": u.script_hex,
                    "address": u.address,
                    "height": u.height,
                    "complete": u.complete,
                }
                for u in history.utxos(tax_account_id)
            ]
        }


def _import_routes(app: FastAPI, imports: Imports, authenticated: list[Any]) -> None:
    @app.get("/api/accounts", dependencies=authenticated)
    def overview() -> dict[str, Any]:
        o = imports.overview()
        return {
            "online": imports.online,
            "entities": [
                {"id": e.id, "name": e.name, "kind": e.kind, "knows_identity": e.knows_identity}
                for e in o.entities
            ],
            "tax_accounts": [
                {"id": a.id, "name": a.name, "kind": a.kind, "entity_id": a.entity_id} for a in o.tax_accounts
            ],
            "clients": [{"id": c.id, "name": c.name, "kind": c.kind} for c in o.clients],
        }

    @app.post("/api/entities", status_code=201, dependencies=authenticated)
    def add_entity(body: NewEntity) -> dict[str, int]:
        return {"id": imports.add_entity(body.name, body.kind)}

    @app.post("/api/tax-accounts", status_code=201, dependencies=authenticated)
    def add_tax_account(body: NewTaxAccount) -> dict[str, int]:
        return {"id": imports.add_tax_account(body.name, body.kind, entity_id=body.entity_id)}

    @app.post("/api/clients", status_code=201, dependencies=authenticated)
    def add_client(body: NewClient) -> dict[str, int]:
        return {"id": imports.add_client(body.name, body.kind)}

    @app.post("/api/imports/addresses/preview", dependencies=authenticated)
    def preview_addresses(body: Upload) -> dict[str, Any]:
        p = imports.preview_addresses(body.text)
        return {
            "new": [{"script": s, "address": a} for s, a in p.new],
            "known": _known(p.known),
            "repeated": p.repeated,
            "invalid_lines": list(p.invalid_lines),
        }

    @app.post("/api/imports/addresses", dependencies=authenticated)
    def import_addresses(body: OwnedUpload) -> dict[str, Any]:
        result = imports.import_addresses(body.text, body.owner())
        return {"added": list(result.added), "conflicts": list(result.conflicts)}

    @app.post("/api/imports/descriptor/preview", dependencies=authenticated)
    def preview_descriptor(body: DescriptorUpload) -> dict[str, Any]:
        p = imports.preview_descriptor(body.text, body.gap_limit)
        return {
            "descriptor": p.info.text,
            "ranged": p.info.is_range,
            "gap_limit": p.gap_limit,
            "derived": [{"index": i, "script": s, "address": a} for i, s, a in p.derived],
            "already_imported": p.already_imported,
            "known": _known(p.known),
        }

    @app.post("/api/imports/descriptor", status_code=201, dependencies=authenticated)
    def import_descriptor(body: OwnedDescriptorUpload) -> dict[str, int]:
        return {"id": imports.import_descriptor(body.text, body.owner(), body.gap_limit)}
