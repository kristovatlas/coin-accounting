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

FastAPI's docs, OpenAPI schema and default validation errors are off: the docs page loads
third-party assets (T-106), and the default error body echoes the request input.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from coinacct.api.placeholder import APP_JS, INDEX_HTML
from coinacct.api.security import SecurityMiddleware
from coinacct.api.session import ClaimError, Sessions
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


def create_app(
    *,
    port: int,
    sessions: Sessions,
    status: Callable[[], NodeStatus],
    on_quit: Callable[[], None],
    bundle: Mapping[str, bytes] | None = None,
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

    return SecurityMiddleware(app, port=port)
