"""Host-allow gate for ``--mobile`` mode."""
from __future__ import annotations
import ipaddress
from typing import Iterable
from starlette.types import ASGIApp, Receive, Scope, Send

_ALLOWED_EXACT = frozenset({"/mobile", "/static/favicon.ico"})
_ALLOWED_PREFIXES: tuple[str, ...] = ("/api/mobile/", "/static/mobile/")
_LOOPBACK_ONLY_EXACT = frozenset({"/api/mobile/info"})


def is_loopback_client(host: str | None) -> bool:
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback


def _path_allowed_for_lan(path: str) -> bool:
    if path in _LOOPBACK_ONLY_EXACT:
        return False
    if path in _ALLOWED_EXACT:
        return True
    return any(path.startswith(p) for p in _ALLOWED_PREFIXES)


class HostAllowMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        client = scope.get("client")
        host = client[0] if client else None
        if is_loopback_client(host):
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "") or ""
        if _path_allowed_for_lan(path):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "http":
            await _send_http_404(send)
        else:
            await _reject_websocket(receive, send)


async def _send_http_404(send: Send) -> None:
    await send({"type": "http.response.start", "status": 404,
                "headers": [(b"content-type", b"text/plain; charset=utf-8")]})
    await send({"type": "http.response.body", "body": b"Not Found", "more_body": False})


async def _reject_websocket(receive: Receive, send: Send) -> None:
    message = await receive()
    if message["type"] == "websocket.connect":
        await send({"type": "websocket.close", "code": 1008})