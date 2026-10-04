"""No token storage, outbound requests, or user-supplied redirect destination."""

import json
import os
import re
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import parse_qsl, urlencode

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from . import PROTOCOL, PROTOCOL_VERSION

LOOPBACK = "http://127.0.0.1:8765/oauth/callback"
MAX_BODY = 65536
MAX_QUERY = 4096
HOST_PATTERN = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}\Z")
PORTAL_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.bitrix24\.(?:ru|com|eu|de|by|kz|ua)\Z")
PATH_PATTERN = re.compile(r"(?:/[A-Za-z0-9][A-Za-z0-9_-]*)*\Z")
STATE_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,256}\Z")
FIELDS = {"code", "state", "domain", "member_id", "scope", "server_domain"}


@dataclass(frozen=True)
class RelayConfig:
    public_host: str
    allowed_portals: frozenset[str]
    base_path: str = ""

    def __post_init__(self):
        if not HOST_PATTERN.fullmatch(self.public_host):
            raise ValueError("PUBLIC_HOST must be an exact lower-case HTTPS DNS hostname")
        if not self.allowed_portals or any(not PORTAL_PATTERN.fullmatch(p) for p in self.allowed_portals):
            raise ValueError("ALLOWED_PORTALS must explicitly list supported cloud Bitrix24 hostnames")
        if not PATH_PATTERN.fullmatch(self.base_path):
            raise ValueError("RELAY_BASE_PATH must be empty or an absolute path without trailing slash")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None):
        values = os.environ if env is None else env
        return cls(
            public_host=values.get("PUBLIC_HOST", "").strip(),
            allowed_portals=frozenset(p.strip() for p in values.get("ALLOWED_PORTALS", "").split(",") if p.strip()),
            base_path=values.get("RELAY_BASE_PATH", "").strip(),
        )

    def path(self, endpoint: str):
        return self.base_path + "/oauth/" + endpoint


def unique_pairs(pairs):
    values = {}
    for key, value in pairs:
        if key in values:
            raise ValueError("Duplicate parameter")
        values[key] = value
    return values


def clean_text(value, maximum: int):
    return isinstance(value, str) and 0 < len(value) <= maximum and not any(ord(char) < 32 for char in value)


def create_relay(config: RelayConfig):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)

    @app.middleware("http")
    async def protect(request, call_next):
        # The TLS proxy sets Host to PUBLIC_HOST, never to the upstream address.
        if request.headers.get("host") not in {config.public_host, config.public_host + ":443"}:
            response = Response(status_code=403)
        elif len(request.scope.get("query_string", b"")) > MAX_QUERY:
            response = Response(status_code=414)
        else:
            response = await call_next(request)
        response.headers.update({
            "Cache-Control": "no-store", "Pragma": "no-cache", "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
            "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'; base-uri 'none'",
        })
        return response

    @app.exception_handler(Exception)
    async def failure(request, exc):
        # Never include exception or request text: installation POST bodies carry
        # auth tokens. The process launcher also disables Uvicorn request logging.
        return Response(status_code=400)

    @app.get(config.path("health"))
    async def health(request: Request):
        if request.query_params:
            return Response(status_code=400)
        return JSONResponse({
            "protocol": PROTOCOL, "version": PROTOCOL_VERSION,
            "callback_path": config.path("callback"), "install_path": config.path("install"),
            "loopback": LOOPBACK, "credentials": "desktop-only",
            "allowed_portals": sorted(config.allowed_portals),
        })

    @app.get(config.path("install"))
    async def installation_info(request: Request):
        if request.query_params:
            return Response(status_code=400)
        # An operator opening this address sees its purpose, without triggering
        # an installation. The real no-UI installer is a server-to-server POST.
        return JSONResponse({"protocol": PROTOCOL, "installation": "bitrix-server-post",
            "self_finish": False, "token_storage": "none"})

    @app.post(config.path("install"))
    async def install(request: Request):
        if request.query_params:
            return Response(status_code=400)
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_BODY:
                return Response(status_code=413)
            body.extend(chunk)
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        try:
            if content_type == "application/json":
                payload = json.loads(body, object_pairs_hook=unique_pairs)
                auth = payload.get("auth")
                event = payload.get("event")
                domain = auth.get("domain") if isinstance(auth, dict) else None
            elif content_type == "application/x-www-form-urlencoded":
                payload = unique_pairs(parse_qsl(body.decode("utf-8"), keep_blank_values=True,
                    strict_parsing=True, max_num_fields=100, errors="strict"))
                event, domain = payload.get("event"), payload.get("auth[domain]")
            else:
                return Response(status_code=415)
        except (ValueError, AttributeError, UnicodeError, RecursionError):
            return Response(status_code=400)
        if (not isinstance(event, str) or event not in {"ONAPPINSTALL", "ONAPPUSERREADY"}
            or not isinstance(domain, str) or domain not in config.allowed_portals):
            return Response(status_code=400)
        # auth contains the installer's tokens. They are discarded here, not
        # stored or forwarded. Desktop obtains tokens for the user completing
        # the separate full OAuth flow. No BX24.installFinish call is needed.
        return Response(status_code=200)

    @app.get(config.path("callback"))
    async def callback(request: Request):
        try:
            params = unique_pairs(request.query_params.multi_items())
        except ValueError:
            return Response(status_code=400)
        if any(key not in FIELDS for key in params):
            return Response(status_code=400)
        if (not STATE_PATTERN.fullmatch(params.get("state", ""))
            or params.get("domain") not in config.allowed_portals
            or not clean_text(params.get("code"), 256)
            or not clean_text(params.get("member_id"), 128)
            or params.get("server_domain", "oauth.bitrix.info") != "oauth.bitrix.info"
            or not clean_text(params.get("scope"), 512)
            or "call" not in params["scope"].split(",")):
            return Response(status_code=400)
        # A fixed top-level redirect requires no CORS/private-network fetch.
        # The desktop validates the exact one-use state, portal, scopes and the
        # token exchange result. This relay has no secret with which to redeem code.
        forwarded = {key: params[key] for key in ("code", "state", "domain", "member_id", "scope")}
        return RedirectResponse(LOOPBACK + "?" + urlencode(forwarded), status_code=303)

    return app
