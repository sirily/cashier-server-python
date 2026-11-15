"""Bound request bodies before FastAPI's JSON parser allocates their contents."""

import anyio
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

MAX_REQUEST_BYTES = 1024 * 1024
MAX_QUERY_STRING_BYTES = 64 * 1024
BODY_TIMEOUT_SECONDS = 15


class RequestLimitsMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        lengths = [value for name, value in scope["headers"] if name == b"content-length"]
        if len(lengths) > 1 or (lengths and (len(lengths[0]) > 10 or not lengths[0].isdigit())):
            await JSONResponse({"detail": "Invalid Content-Length"}, status_code=400)(scope, receive, send)
            return
        if (
            lengths and int(lengths[0]) > MAX_REQUEST_BYTES
            or len(scope.get("query_string", b"")) > MAX_QUERY_STRING_BYTES
        ):
            await JSONResponse({"detail": "Request exceeds size limit"}, status_code=413)(scope, receive, send)
            return
        body = bytearray()
        try:
            with anyio.fail_after(BODY_TIMEOUT_SECONDS):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    chunk = message.get("body", b"")
                    if len(body) + len(chunk) > MAX_REQUEST_BYTES:
                        await JSONResponse({"detail": "Request exceeds size limit"}, status_code=413)(scope, receive, send)
                        return
                    body.extend(chunk)
                    if not message.get("more_body", False):
                        break
        except TimeoutError:
            await JSONResponse({"detail": "Request body timed out"}, status_code=408)(scope, receive, send)
            return
        delivered = False

        async def bounded_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, bounded_receive, send)
