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
- Events (bearer; PLAN §2, §7, T-408, T-508), through `services.events.Events`: `GET /api/events`
  (every event with its choice of lots), `POST /api/events` (add), `/api/events/update`,
  `/api/events/remove` and `/api/events/identify` (when, from the app's clock). An unknown event is
  404, a refusal 422 with a fixed message, a busy DB 503.
- Ledger (bearer; PLAN §7, ADR 0021 §1, T-507, T-508), through `services.ledger.Ledger`:
  `GET /api/ledger`, the lot engine's run of every stored event: each disposal's lots, each gift's and
  each move's, the lots held, the late choices with the standing method's figures, what blocks a
  report, and which stored choices weren't used (automatic mode's, and stale ones, to warn about). A
  refusal is a 422 with a fixed message naming at most the event's id, a busy DB 503.
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
import re
from collections.abc import Callable, Mapping
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import AwareDatetime, BaseModel, BeforeValidator, ConfigDict, Field

from coinacct.api.placeholder import APP_JS, INDEX_HTML
from coinacct.api.security import MAX_BODY_BYTES, SecurityMiddleware
from coinacct.api.session import ClaimError, Sessions
from coinacct.domain.chain import MAX_SATS
from coinacct.domain.keys import PrivateKeyError
from coinacct.services import events as event_service
from coinacct.services import imports as import_service
from coinacct.services import ledger as ledger_service
from coinacct.services.graph import Graph, NotFound
from coinacct.services.history import History
from coinacct.services.imports import Busy, ImportRefused, Imports, OfflineError, Owner
from coinacct.services.startup import NodeStatus
from coinacct.services.tags import Tagging

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
    graph: Graph | None = None,
    tagging: Tagging | None = None,
    events: event_service.Events | None = None,
    ledger: ledger_service.Ledger | None = None,
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

    @app.exception_handler(NotFound)
    async def not_found(request: Request, exc: NotFound) -> Response:
        return JSONResponse({"error": "not found"}, status_code=404)

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
    if graph is not None:
        _graph_routes(app, graph, authenticated)
    if tagging is not None:
        _tag_routes(app, tagging, authenticated)
    if events is not None:
        _event_routes(app, events, authenticated)
    if ledger is not None:
        _ledger_routes(app, ledger, authenticated)
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


HASH = r"^[0-9a-f]{64}$"


class TxQuery(Strict):
    txid: str = Field(pattern=HASH)
    blockhash: str | None = Field(default=None, pattern=HASH)  # None: unconfirmed, or let the node find it


class SpenderQuery(TxQuery):
    n: int = Field(ge=0, le=100_000)


def _owner(o: Any) -> dict[str, Any] | None:
    return (
        None
        if o is None
        else {
            "entity_id": o.entity_id,
            "tax_account_id": o.tax_account_id,
            "label": o.label,
            "client_ids": list(o.client_ids),
        }
    )


# POST, with txids in the body: which transactions the user expands would otherwise sit in URLs the
# browser may keep, outside the volume (T-105).
def _graph_routes(app: FastAPI, graph: Graph, authenticated: list[Any]) -> None:
    @app.post("/api/graph/tx", dependencies=authenticated)
    def tx(body: TxQuery) -> dict[str, Any]:
        found = graph.tx(body.txid, body.blockhash)
        return {
            "txid": found.txid,
            "blockhash": found.blockhash,
            "confirmations": found.confirmations,
            "block_time": found.block_time,
            "mixing": found.mixing,
            "inputs": [
                {
                    "prevout": None
                    if i.prevout is None
                    else {"txid": i.prevout.txid, "vout": i.prevout.vout},
                    "sats": i.sats,
                    "script": i.script_hex,
                    "address": i.address,
                    "owner": _owner(i.owner),
                }
                for i in found.inputs
            ],
            "outputs": [
                {
                    "n": o.n,
                    "sats": o.sats,
                    "script": o.script_hex,
                    "address": o.address,
                    "unspendable": o.unspendable,
                    "owner": _owner(o.owner),
                }
                for o in found.outputs
            ],
        }

    @app.post("/api/graph/spender", dependencies=authenticated)
    def spender(body: SpenderQuery) -> dict[str, Any]:
        found = graph.spender(body.txid, body.blockhash, body.n)
        return {
            "state": found.state,
            "spending_txid": found.spending_txid,
            "blockhash": found.blockhash,
            "as_of": _tip(found.as_of),
        }


RowId = Annotated[
    int, Field(strict=True, ge=1, le=2**63 - 1)
]  # SQLite's integer range: no OverflowError, no 500


class AddressTag(Strict):
    script: str = Field(pattern=r"^(?:[0-9a-f]{2}){1,10000}$")
    address: str | None = Field(default=None, min_length=1, max_length=90)
    entity_id: RowId
    tax_account_id: RowId | None = None
    label: str = Field(default="", max_length=200)
    client_ids: list[RowId] = Field(default_factory=list, max_length=100)


class MixingFlag(Strict):
    txid: str = Field(pattern=HASH)
    mixing: bool


class Subject(Strict):
    kind: Literal["address_tag", "tx_flag"]  # a 32-byte script and a txid look alike
    subject: str = Field(pattern=r"^(?:[0-9a-f]{2}){1,10000}$")  # a script or a txid


USD = Annotated[str, Field(pattern=r"^-?[0-9]{1,15}(\.[0-9]{1,2})?$")]  # exact cents, as text


_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)  # ASCII digits only
_ISO_MOMENT = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})", re.ASCII
)


def _iso_date(value: object) -> object:
    """A date is ISO 8601 text: pydantic would read a number, or a number as text, as a Unix time."""
    if not isinstance(value, str) or not _ISO_DATE.fullmatch(value):
        raise ValueError("an ISO 8601 date")
    return value


def _iso_moment(value: object) -> object:
    """A moment is ISO 8601 text with its zone, never a Unix time in any form."""
    if not isinstance(value, str) or not _ISO_MOMENT.fullmatch(value):
        raise ValueError("an ISO 8601 moment")
    return value


class EventBody(Strict):
    kind: Literal[
        "buy",
        "p2p_buy",
        "income",
        "gift_in",
        "inherit",
        "self_transfer",
        "deposit",
        "withdrawal",
        "sell",
        "spend",
        "gift_out",
    ]
    account: RowId
    to: RowId | None = None
    at: Annotated[AwareDatetime, BeforeValidator(_iso_moment)]
    sats: int = Field(strict=True, ge=1, le=MAX_SATS)
    usd: USD | None = None
    fee_sats: int = Field(default=0, strict=True, ge=0, le=MAX_SATS)
    valuation_source: str | None = Field(default=None, min_length=1, max_length=200)
    txid: str | None = Field(default=None, pattern=HASH)
    fmv: USD | None = None
    donor_acquired: Annotated[date, BeforeValidator(_iso_date)] | None = None
    donor_always_long: bool = Field(default=False, strict=True)
    missing_basis: USD | None = None
    missing_acquired: Annotated[date, BeforeValidator(_iso_date)] | None = None
    note: str | None = Field(default=None, min_length=1, max_length=2000)

    def event(self) -> event_service.Event:
        def money(v: str | None) -> Decimal | None:
            return None if v is None else Decimal(v)

        return event_service.Event(
            self.kind,
            self.account,
            self.at,
            self.sats,
            money(self.usd),
            self.to,
            self.fee_sats,
            self.valuation_source,
            self.txid,
            money(self.fmv),
            self.donor_acquired,
            self.donor_always_long,
            money(self.missing_basis),
            self.missing_acquired,
            self.note,
        )


class EventEdit(Strict):
    id: RowId
    event: EventBody


class EventRef(Strict):
    id: RowId


class ChoiceBody(Strict):
    lot: str = Field(pattern=r"^[0-9A-Za-z_.:@#-]{1,400}$")  # the engine's lot ids
    sats: int = Field(strict=True, ge=1, le=MAX_SATS)


class Identification(Strict):
    """A choice of lots made now: the app records when (ADR 0008 §3), so the body doesn't say."""

    id: RowId
    choices: list[ChoiceBody] = Field(max_length=1000)


def _event_json(
    stored: event_service.Stored, choice: tuple[tuple[event_service.Choice, ...], datetime | None]
) -> dict[str, Any]:
    e = stored.event
    choices, chosen_at = choice
    return {
        "id": stored.id,
        "kind": e.kind,
        "account": e.account,
        "to": e.to,
        "at": e.at.isoformat(),
        "sats": e.sats,
        "usd": None if e.usd is None else str(e.usd),
        "fee_sats": e.fee_sats,
        "valuation_source": e.valuation_source,
        "txid": e.txid,
        "fmv": None if e.fmv is None else str(e.fmv),
        "donor_acquired": None if e.donor_acquired is None else e.donor_acquired.isoformat(),
        "donor_always_long": e.donor_always_long,
        "missing_basis": None if e.missing_basis is None else str(e.missing_basis),
        "missing_acquired": None if e.missing_acquired is None else e.missing_acquired.isoformat(),
        "note": e.note,
        "choices": [{"lot": c.lot, "sats": c.sats} for c in choices],
        "identified_at": None if chosen_at is None else chosen_at.isoformat(),
    }


def _event_routes(app: FastAPI, service: event_service.Events, authenticated: list[Any]) -> None:
    @app.get("/api/events", dependencies=authenticated)
    def list_events() -> dict[str, Any]:
        return {"events": [_event_json(s, c) for s, c in service.list()]}

    @app.post("/api/events", status_code=201, dependencies=authenticated)
    def add_event(body: EventBody) -> dict[str, int]:
        return {"id": service.add(body.event())}

    @app.post("/api/events/update", dependencies=authenticated)
    def update_event(body: EventEdit) -> dict[str, int]:
        service.update(body.id, body.event.event())
        return {"id": body.id}

    @app.post("/api/events/remove", dependencies=authenticated)
    def remove_event(body: EventRef) -> dict[str, int]:
        service.remove(body.id)
        return {"id": body.id}

    @app.post("/api/events/identify", dependencies=authenticated)
    def identify(body: Identification) -> dict[str, int]:
        choices = tuple(event_service.Choice(c.lot, c.sats) for c in body.choices)
        service.identify(body.id, choices)
        return {"id": body.id}


def _usd(amount: Decimal) -> str:
    return format(amount, "f")  # never an exponent


def _part(part: Any) -> dict[str, Any]:
    """A disposal's lot, a gift given's or a move's, as the ledger and the late warnings show them."""
    out: dict[str, Any] = {"lot": part.lot, "sats": part.sats, "basis": _usd(part.basis)}
    out["acquired"] = part.acquired.isoformat()
    if isinstance(part, ledger_service.Allocation):
        out |= {
            "event": int(part.disposal),
            "proceeds": _usd(part.proceeds),
            "disposed": part.disposed.isoformat(),
            "long_term": part.long_term,
            "rule": part.rule,
            "lot_basis": _maybe_usd(part.lot_basis),
            "lot_loss_basis": _maybe_usd(part.lot_loss_basis),
        }
    elif isinstance(part, ledger_service.GiftGiven):
        out |= {
            "event": int(part.disposal),
            "unknown_basis": part.unknown_basis,
            "always_long": part.always_long,
        }
    elif isinstance(part, ledger_service.Moved):
        out |= {
            "event": int(part.transfer),
            "into": part.into,
            "to": int(part.to),
            "carried_fee": part.carried_fee,
            "carried_from": list(part.carried_from),
        }
    return out


def _maybe_usd(amount: Decimal | None) -> str | None:
    return None if amount is None else _usd(amount)


def _lot(lot: Any) -> dict[str, Any]:
    return {
        "lot": lot.id,
        "account": int(lot.account),
        "acquired": lot.acquired.isoformat(),
        "sats": lot.sats,
        "basis": _usd(lot.basis),
        "loss_basis": _maybe_usd(lot.loss_basis),
        "loss_from": None if lot.loss_from is None else lot.loss_from.isoformat(),
        "always_long": lot.always_long,
        "unknown_basis": lot.unknown_basis,
    }


def _ledger_json(run: ledger_service.Run) -> dict[str, Any]:
    r = run.result
    return {
        "automatic": list(run.automatic),
        "stale": list(run.stale),
        "allocations": [_part(a) for a in r.allocations],
        "gifts": [_part(g) for g in r.gifts],
        "moves": [_part(m) for m in r.moves],
        "holdings": [_lot(lot) for lot in r.holdings],
        "created": [_lot(lot) for lot in r.created],  # lots made for a withdrawal's unrecorded sats
        "late": [
            {
                "event": int(late.disposal),
                "identified_at": late.identified_at.isoformat(),
                "replayed": late.replayed,
                "standing": [_part(p) for p in late.standing],
            }
            for late in r.late
        ],
        "blocking": [
            {"kind": "missing_lots", "event": int(b.disposal), "sats": b.sats, "proceeds": _usd(b.proceeds)}
            if isinstance(b, ledger_service.MissingLots)
            else {"kind": "unknown_basis", "lot": b.lot}
            for b in r.blocking
        ],
        "replay_stopped": None if r.replay_stopped is None else int(r.replay_stopped),
    }


def _ledger_routes(app: FastAPI, ledger: ledger_service.Ledger, authenticated: list[Any]) -> None:
    @app.get("/api/ledger", dependencies=authenticated)
    def run_ledger() -> dict[str, Any]:
        return _ledger_json(ledger.run())


# POST bodies, as for the graph: scripts and txids stay out of URLs (T-105).
def _tag_routes(app: FastAPI, tagging: Tagging, authenticated: list[Any]) -> None:
    @app.post("/api/tags/address", dependencies=authenticated)
    def tag_address(body: AddressTag) -> dict[str, Any]:
        new = tagging.tag_address(
            body.script,
            text=body.address,
            entity_id=body.entity_id,
            tax_account_id=body.tax_account_id,
            label=body.label,
            client_ids=body.client_ids,
        )
        return {"new": new}

    @app.post("/api/tags/mixing", dependencies=authenticated)
    def set_mixing(body: MixingFlag) -> dict[str, Any]:
        tagging.set_mixing(body.txid, body.mixing)
        return {"mixing": body.mixing}

    @app.post("/api/tags/history", dependencies=authenticated)
    def history(body: Subject) -> dict[str, Any]:
        return {
            "changes": [
                {"at": c.at, "kind": c.kind, "before": c.before, "after": c.after, "origin": c.origin}
                for c in tagging.changes(body.kind, body.subject)
            ]
        }
