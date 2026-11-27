"""
Cashier Server - Beancount synchronization server for Cashier PWA
FastAPI implementation
"""

import hmac
import os
import re
import sys
import threading
from pathlib import Path
from typing import Annotated

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from loguru import logger
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from cashier_snapshot.api_contract import (
    BEARER_DEPENDENCIES,
    HEALTH_RESPONSES,
    PROTECTED_ERRORS,
    SERVERS,
    SNAPSHOT_DESCRIPTION,
    SNAPSHOT_RESPONSES,
    XACT_DESCRIPTION,
    XACT_RESPONSES,
    install_openapi_contract,
)
from cashier_snapshot.http_limits import RequestLimitsMiddleware

_heavy_operations = threading.BoundedSemaphore(4)
_HOST_RE = re.compile(r"(?:[A-Za-z0-9._-]+|\[[0-9A-Fa-f:.]+\])(?::[0-9]{1,5})?\Z")


def run_heavy_operation(operation, *args):
    if not _heavy_operations.acquire(blocking=False):
        raise HTTPException(status_code=503, detail="Server is busy; retry later")
    try:
        return operation(*args)
    finally:
        _heavy_operations.release()

# Exception traces must not copy ledger data or request locals into server logs.
logger.configure(handlers=[{"sink": sys.stderr, "diagnose": False, "backtrace": False}])

load_dotenv()
BEAN_FILE = os.getenv("BEANCOUNT_FILE")
CASHIER_API_TOKEN = os.getenv("CASHIER_" + "API_TOKEN", "").strip()

# Create a FastAPI instance
app = FastAPI(
    title="Cashier Server",
    description="Beancount synchronization server for Cashier PWA",
    version="0.14.1",
    servers=SERVERS,
)
install_openapi_contract(app)


@app.middleware("http")
async def require_api_token(request: Request, call_next):
    """Require one deployment-scoped bearer token for application API routes."""
    hosts = request.headers.getlist("host")
    if len(hosts) != 1 or _HOST_RE.fullmatch(hosts[0]) is None:
        return JSONResponse(status_code=400, content={"detail": "Invalid Host header"})

    # Never reconstruct the authentication path from the attacker-controlled Host.
    if request.scope.get("path") == "/health":
        return await call_next(request)

    if not CASHIER_API_TOKEN:
        return JSONResponse(
            status_code=503,
            content={"detail": "Cashier API token is not configured"},
        )

    authorization = request.headers.get("authorization", "")
    scheme, separator, token = authorization.partition(" ")
    valid = (
        separator == " "
        and scheme.lower() == "bearer"
        and bool(token)
        and hmac.compare_digest(token.encode("utf-8"), CASHIER_API_TOKEN.encode("utf-8"))
    )
    if not valid:
        return JSONResponse(
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
            content={"detail": "Invalid or missing API token"},
        )

    return await call_next(request)


# Outermost middleware: enforce body/query limits before authentication and JSON parsing.
app.add_middleware(RequestLimitsMiddleware)



@app.get(
    "/ping",
    summary="Authenticated liveness check",
    description="Requires the deployment bearer token; returns the JSON string \"pong\".",
    dependencies=BEARER_DEPENDENCIES,
    response_model=None,
    responses={
        **PROTECTED_ERRORS,
        200: {"description": "JSON string pong.", "content": {"application/json": {
            "schema": {"type": "string", "const": "pong"}, "example": "pong",
        }}},
    },
)
async def ping():
    """
    Simple ping endpoint to check if the server is running.
    """
    return "pong"


@app.get(
    "/health",
    summary="Public lightweight health check",
    description="No bearer token required. Reports whether BEANCOUNT_FILE is configured without reading or validating the ledger.",
    response_model=None,
    responses=HEALTH_RESPONSES,
    openapi_extra={"security": []},
)
async def health():
    """Return a small health payload suitable for container health checks."""
    return {
        "ok": True,
        "beancount_file_configured": bool(BEAN_FILE),
    }



def reject_unsafe_infrastructure_path(file_path: str) -> Path:
    if len(file_path) > 1024:
        raise HTTPException(status_code=400, detail="File path exceeds length limit")
    if any(ord(char) < 32 for char in file_path):
        raise HTTPException(status_code=400, detail="Control characters are not allowed")

    requested_path = Path(file_path)
    if not requested_path.parts:
        raise HTTPException(status_code=404, detail="File not found")
    if requested_path.is_absolute():
        raise HTTPException(status_code=403, detail="Absolute paths are not allowed")
    if any(part.startswith(".") for part in requested_path.parts):
        raise HTTPException(status_code=403, detail="Hidden paths and path traversal are not allowed")

    return requested_path



def render_materialized_root_book() -> str:
    """Build a standalone source-preserving root ledger for the PWA.

    The PWA consumes /infrastructure as Beancount text and parses it offline.
    Python plugins are executed by Beancount on the server; the snapshot builder
    keeps unchanged source directives verbatim and prints only generated or
    proven-safe transformed entries.
    """
    if not BEAN_FILE:
        raise HTTPException(status_code=500, detail="BEANCOUNT_FILE environment variable not set")

    from cashier_snapshot import SnapshotBuildError, StandaloneSnapshotBuilder
    from cashier_snapshot.printer import print_generated_entries_for_pwa

    try:
        content, stats = StandaloneSnapshotBuilder(
            Path(BEAN_FILE),
            print_generated=print_generated_entries_for_pwa,
        ).build()
    except SnapshotBuildError as exc:
        error = str(exc)
        message = "Standalone PWA snapshot could not be built"
        if error.startswith("Beancount root book could not be materialized:"):
            message = "Beancount root book could not be materialized"
        raise HTTPException(
            status_code=422,
            detail={
                "message": message,
                "errors": [error],
            },
        ) from exc

    logger.info(
        f"Built standalone PWA snapshot: retained={stats.retained}, "
        f"omitted_infrastructure={stats.omitted_infrastructure}, "
        f"consumed_operational={stats.consumed_operational}, "
        f"generated={stats.generated}, transformed={stats.transformed}, "
        f"bytes={len(content.encode('utf-8'))}"
    )
    return content


def read_infrastructure_file(file_path: str):
    """Return only the configured root snapshot, never client-selected source."""
    requested_path = reject_unsafe_infrastructure_path(file_path)
    if not BEAN_FILE:
        raise HTTPException(status_code=500, detail="BEANCOUNT_FILE environment variable not set")
    if any(char in file_path for char in "*?[") or requested_path != Path(Path(BEAN_FILE).name):
        raise HTTPException(status_code=403, detail="Only the configured root snapshot is available")
    return {"content": render_materialized_root_book()}


@app.get(
    "/infrastructure",
    summary="Export the configured root ledger snapshot",
    description=SNAPSHOT_DESCRIPTION,
    dependencies=BEARER_DEPENDENCIES,
    response_model=None,
    responses=SNAPSHOT_RESPONSES,
)
async def infrastructure_file(file_path: Annotated[str, Query(
    description="Required configured BEANCOUNT_FILE basename only; no arbitrary file access.",
    examples=["main.bean"],
)]):
    return await run_in_threadpool(run_heavy_operation, read_infrastructure_file, file_path)



class XactRequest(BaseModel):
    transactions: list[str] = Field(
        max_length=100,
        description="Up to 100 completed * Beancount transaction strings, each with stable cashier_id metadata.",
    )


class XactRejected(BaseModel):
    cashier_id: str | None = Field(description="Null when no valid ID could be recovered.")
    reason: str = Field(description="Concrete per-item validation or conflict reason.")


class XactResponse(BaseModel):
    synchronized: list[str] = Field(description="Newly committed and already-equivalent cashier_ids.")
    rejected: list[XactRejected] = Field(description="Per-item rejections; mixed batches still return HTTP 200.")



@app.post(
    "/xact",
    summary="Synchronize Cashier manual transactions",
    description=XACT_DESCRIPTION,
    dependencies=BEARER_DEPENDENCIES,
    response_model=XactResponse,
    responses=XACT_RESPONSES,
)
async def write_xact(request: XactRequest):
    """
    Accept Cashier-created manual transaction directives and append them to
    the configured BEANCOUNT_MANUAL_TRANSACTIONS_FILE.

    This is NOT a general Beancount mutation API. Only completed ``*``
    transaction directives with ``cashier_id`` metadata are accepted.

    Returns synchronized cashier_ids and any rejections with reasons.
    """
    manual_file = os.getenv("BEANCOUNT_MANUAL_TRANSACTIONS_FILE")
    if not manual_file:
        raise HTTPException(
            status_code=500,
            detail="BEANCOUNT_MANUAL_TRANSACTIONS_FILE is not configured",
        )

    from writeback import validate_and_commit

    try:
        result, _wrote = await run_in_threadpool(run_heavy_operation, validate_and_commit, request.transactions)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


    return XactResponse(
        synchronized=result["synchronized"],
        rejected=[
            XactRejected(cashier_id=r["cashier_id"], reason=r["reason"])
            for r in result["rejected"]
        ],
    )


def main():
    """
    Entry point for the executable script.
    """
    if not CASHIER_API_TOKEN:
        raise RuntimeError("CASHIER_API_TOKEN must be configured")

    logger.info("Starting Cashier Server on 0.0.0.0:3000")

    if not BEAN_FILE:
        raise RuntimeError("BEANCOUNT_FILE must be configured")


    config = uvicorn.Config(
        app, host="0.0.0.0", port=3000,
        limit_concurrency=64,
    )
    server = uvicorn.Server(config)
    server.run()


if __name__ == "__main__":
    main()
