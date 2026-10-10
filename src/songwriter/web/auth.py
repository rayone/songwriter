"""HTTP Basic auth for the whole app, as plain ASGI middleware.

Plain ASGI (not BaseHTTPMiddleware) so streaming responses -- the cowriter's
server-sent events -- pass through untouched. ``/healthz`` stays open so load
balancers and systemd checks need no credentials.
"""

from __future__ import annotations

import base64
import binascii
import hmac
from typing import Any, Awaitable, Callable

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

OPEN_PATHS = frozenset({"/healthz"})


class BasicAuthMiddleware:
    def __init__(self, app: ASGIApp, username: str, password: str, realm: str = "Songwriter") -> None:
        self.app = app
        self._user = username.encode()
        self._password = password.encode()
        self._realm = realm

    def _authorized(self, header: bytes | None) -> bool:
        if not header or not header[:6].lower() == b"basic ":
            return False
        try:
            user, _, password = base64.b64decode(header[6:].strip(), validate=True).partition(b":")
        except (binascii.Error, ValueError):
            return False
        # Compare both fields in constant time; evaluate both to avoid a timing leak.
        ok_user = hmac.compare_digest(user, self._user)
        ok_pass = hmac.compare_digest(password, self._password)
        return ok_user and ok_pass

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket") or scope.get("path") in OPEN_PATHS:
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        if self._authorized(headers.get(b"authorization")):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"www-authenticate", f'Basic realm="{self._realm}", charset="UTF-8"'.encode()),
                (b"content-type", b"text/plain; charset=utf-8"),
            ],
        })
        await send({"type": "http.response.body", "body": b"Authentication required\n"})
