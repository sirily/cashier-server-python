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

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from loguru import logger
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

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
    version="0.14.0",
)


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


@app.get("/")
async def index(query: str | None = None):
    """Execute a BQL query against the configured Beancount book."""
    if not query:
        return {"error": "No query provided"}

    return await beancount(query)


def preload_beancount_data():
    """
    Pre-load beancount data into memory.
    """
    import beanquery
    from beancount import loader

    from cashier_snapshot.workspace import private_ledger_copy

    if not BEAN_FILE:
        raise ValueError("BEANCOUNT_FILE must be configured")

    with private_ledger_copy(Path(BEAN_FILE)) as root:
        entries, errors, options = loader.load_file(str(root))
    if errors:
        raise ValueError("Beancount ledger has validation errors")
    connection = beanquery.connect("beancount:", entries=entries, errors=[], options=options)
    # Query workers extract entries/options, never a DSN or mutable table registry.
    return connection


async def beancount(query: str | None = None):
    """
    Execute a beancount query and return the result.
    Requires Beancount to be installed.

    Args:
        query: The beancount command to execute

    Returns:
        The result of the beancount command
    """
    import beancount

    from cashier_snapshot.query import QueryRejected, QueryTimeout, execute_readonly

    if not BEAN_FILE:
        raise HTTPException(status_code=500, detail="BEANCOUNT_FILE must be configured")
    if not query:
        return {"error": "No query provided"}

    connection = app.state.connection
    try:
        result = await run_in_threadpool(run_heavy_operation, execute_readonly, connection, query)
    except QueryTimeout as exc:
        raise HTTPException(status_code=422, detail="BQL query exceeded execution limit") from exc
    except QueryRejected as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # convert Inventory objects (sets) into lists that are JSON-serializable.
    for i, row in enumerate(result):
        # Convert the row to a list
        row_list = list(row)
        # Iterate over the tuple values
        for j, value in enumerate(row_list):
            if isinstance(value, beancount.Inventory):
                # Turn the Inventory into a simple list
                value = list(value)
                row_list[j] = value
        # Convert the list back to a tuple and set it back into the result
        result[i] = tuple(row_list)

    return result


@app.get("/reload")
def reload_beancount_data():
    """
    Reload the Beancount data.
    """
    if not BEAN_FILE:
        raise HTTPException(status_code=500, detail="BEANCOUNT_FILE must be configured")
    try:
        app.state.connection = run_heavy_operation(preload_beancount_data)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Beancount ledger could not be loaded safely") from exc

@app.get("/ping")
async def ping():
    """
    Simple ping endpoint to check if the server is running.
    """
    return "pong"


@app.get("/health")
async def health():
    """Return a small health payload suitable for container health checks."""
    # This endpoint is intentionally lightweight: it reports whether startup
    # loaded Beancount data, but does not execute a query on every health probe.
    return {
        "ok": True,
        "beancount_file_configured": bool(BEAN_FILE),
        "beancount_loaded": hasattr(app.state, "connection"),
    }


def get_ledger_root() -> Path:
    if not BEAN_FILE:
        raise HTTPException(status_code=500, detail="BEANCOUNT_FILE environment variable not set")

    return Path(os.path.abspath(BEAN_FILE)).parent


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


def resolve_infrastructure_path(file_path: str) -> Path:
    """Resolve an infrastructure path under the Beancount file directory.

    Cashier PWA asks for files such as config.bean/accounts.bean relative to the
    Beancount book. Do not allow absolute paths or ../ traversal outside the
    ledger directory.
    """
    requested_path = reject_unsafe_infrastructure_path(file_path)
    ledger_root = get_ledger_root()
    return ledger_root / requested_path


def is_root_infrastructure_path(full_file_path: Path) -> bool:
    if not BEAN_FILE:
        return False
    return full_file_path == Path(os.path.abspath(BEAN_FILE))


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


def is_glob_path(file_path: str) -> bool:
    return any(char in file_path for char in "*?[")


def infrastructure_read_error(exc: ValueError) -> HTTPException:
    message = str(exc)
    status = 413 if "budget exceeded" in message else 403
    return HTTPException(status_code=status, detail=message)


def read_infrastructure_glob(file_path: str):
    """Read bounded nonhidden Beancount files without following symlinks."""
    from cashier_snapshot.workspace import LedgerWorkspaceError, glob_workspace_files

    requested_path = reject_unsafe_infrastructure_path(file_path)
    ledger_root = get_ledger_root()
    try:
        files = glob_workspace_files(ledger_root, requested_path.as_posix())
    except LedgerWorkspaceError as exc:
        raise infrastructure_read_error(exc) from exc
    if not files:
        raise HTTPException(status_code=404, detail="File not found: no matching Beancount files")
    try:
        return {
            "files": [
                {
                    "path": path.as_posix(),
                    "content": content.decode("utf-8"),
                }
                for path, content in files.items()
            ]
        }
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=422, detail="Source file is not UTF-8") from exc


def read_infrastructure_file(file_path: str):
    """
    Provides a Beancount infrastructure file.
    For use with RustLedger.
    
    Args:
        file_path: The file path relative to the Beancount directory (e.g., "config.bean", "accounts.bean", "commodities.bean")
    
    Returns:
        The content of the requested file
    """
    if is_glob_path(file_path):
        return read_infrastructure_glob(file_path)

    from cashier_snapshot.workspace import LedgerWorkspaceError, read_workspace_file

    full_file_path = resolve_infrastructure_path(file_path)
    if is_root_infrastructure_path(full_file_path):
        return {"content": render_materialized_root_book()}
    try:
        content = read_workspace_file(get_ledger_root(), Path(file_path)).decode("utf-8")
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Beancount file not found") from exc
    except LedgerWorkspaceError as exc:
        raise infrastructure_read_error(exc) from exc
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=422, detail="Source file is not UTF-8") from exc
    return {"content": content}


@app.get("/infrastructure")
async def infrastructure_file(file_path: str):
    return await run_in_threadpool(run_heavy_operation, read_infrastructure_file, file_path)


def refresh_beancount_connection():
    """Reload the in-memory Beancount connection so subsequent queries see new data."""
    app.state.connection = preload_beancount_data()
    logger.info("Beancount connection refreshed after writeback")


class XactRequest(BaseModel):
    transactions: list[str] = Field(max_length=100)


class XactRejected(BaseModel):
    cashier_id: str | None = None
    reason: str


class XactResponse(BaseModel):
    synchronized: list[str]
    rejected: list[XactRejected]


def commit_and_refresh(transactions: list[str]):
    """Keep the admission slot until a successful write is visible to BQL."""
    from writeback import validate_and_commit

    result, wrote = validate_and_commit(transactions)
    if wrote:
        try:
            refresh_beancount_connection()
        except Exception:
            # The transaction is already durable; never report it as uncommitted.
            logger.exception("Beancount connection refresh failed after writeback")
    return result


@app.post("/xact")
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

    try:
        result = await run_in_threadpool(run_heavy_operation, commit_and_refresh, request.transactions)
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

    app.state.connection = preload_beancount_data()

    config = uvicorn.Config(
        app, host="0.0.0.0", port=3000,
        limit_concurrency=64,
    )
    server = uvicorn.Server(config)
    server.run()


if __name__ == "__main__":
    main()
