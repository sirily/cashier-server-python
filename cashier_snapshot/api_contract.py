"""Declarative OpenAPI metadata; response documentation does not re-encode ledger data."""

from typing import Any

from fastapi import Depends, FastAPI
from fastapi.security import HTTPBearer
from pydantic import BaseModel, Field, JsonValue

from cashier_snapshot.http_limits import (
    BODY_TIMEOUT_SECONDS,
    MAX_QUERY_STRING_BYTES,
    MAX_REQUEST_BYTES,
)

CONTRACT_VERSION = "2.0.0"
SERVERS = [
    {"url": "/", "description": "Server-relative paths; external prefix and proxy limits are deployment configuration."},
]
# Middleware remains authoritative; this non-rejecting dependency declares security only.
BEARER_DEPENDENCIES = [Depends(HTTPBearer(auto_error=False, scheme_name="CashierBearer"))]
CASHIER_LIMITS = {
    "request_body_bytes": MAX_REQUEST_BYTES,
    "query_string_bytes": MAX_QUERY_STRING_BYTES,
    "body_receive_seconds": BODY_TIMEOUT_SECONDS,

    "heavy_operation_slots": 4,
    "uvicorn_concurrency": 64,
    "xact_batch_items": 100,
    "xact_transaction_bytes": 64 * 1024,
    "xact_postings": 256,
    "xact_metadata_fields": 64,
    "xact_metadata_key_characters": 64,
    "xact_metadata_string_bytes": 1024,
    "cashier_id_characters": 128,
    "snapshot_path_characters": 1024,
    "ledger_file_bytes": 16 * 1024 * 1024,
    "ledger_capture_bytes": 64 * 1024 * 1024,
    "ledger_files": 4096,
    "ledger_include_visits": 4096,
    "ledger_include_depth": 64,
    "ledger_expanded_bytes": 64 * 1024 * 1024,
}


class ErrorResponse(BaseModel):
    detail: str


class ValidationIssue(BaseModel):
    loc: list[str | int]
    msg: str
    type: str
    input: JsonValue | None = None
    ctx: dict[str, JsonValue] | None = None


class ValidationErrorResponse(BaseModel):
    detail: list[ValidationIssue]



class SnapshotBuildDetail(BaseModel):
    message: str
    errors: list[str]


class SnapshotBuildErrorResponse(BaseModel):
    detail: SnapshotBuildDetail | list[ValidationIssue]



class SnapshotResponse(BaseModel):
    content: str = Field(description="Standalone source-preserving Beancount ledger text.")


class HealthResponse(BaseModel):
    ok: bool
    beancount_file_configured: bool



PUBLIC_ERRORS: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorResponse, "description": "Invalid Host or Content-Length; malformed request."},
    408: {"model": ErrorResponse, "description": "Request body receive deadline exceeded (15 seconds)."},
    413: {"model": ErrorResponse, "description": "Body exceeds 1 MiB or query string exceeds 64 KiB."},
}
HEALTH_RESPONSES: dict[int | str, dict[str, Any]] = {
    **PUBLIC_ERRORS,
    200: {"model": HealthResponse},
    503: {
        "description": "Uvicorn connection/task admission limit exceeded, before the application handles health.",
        "content": {"text/plain": {"schema": {"type": "string"}}},
    },
}
PROTECTED_ERRORS: dict[int | str, dict[str, Any]] = {
    **PUBLIC_ERRORS,
    401: {
        "model": ErrorResponse,
        "description": "Missing or invalid deployment-scoped bearer token.",
        "headers": {"WWW-Authenticate": {"schema": {"type": "string", "const": "Bearer"}}},
    },
    503: {
        "model": ErrorResponse,
        "description": (
            "Server token not configured or all four heavy-operation slots busy. "
            "Uvicorn may instead return plain text when its 64-connection concurrency limit is reached."
        ),
        "content": {"text/plain": {"schema": {"type": "string"}}},
    },
}
PIPELINE_ERROR: dict[str, Any] = {
    "model": ErrorResponse,
    "description": "Missing server configuration or a safe ledger pipeline/filesystem failure.",
}

SNAPSHOT_RESPONSES: dict[int | str, dict[str, Any]] = {
    **PROTECTED_ERRORS,
    200: {"model": SnapshotResponse, "description": "Configured root snapshot, not arbitrary file access."},
    400: {"model": ErrorResponse, "description": "Invalid request, control characters, or path longer than 1024 characters."},
    403: {"model": ErrorResponse, "description": "Absolute, hidden, traversal, glob, or non-root path rejected."},
    404: {"model": ErrorResponse, "description": "Empty path has no file component."},
    422: {
        "model": SnapshotBuildErrorResponse,
        "description": "Snapshot build failure (detail.message/errors), or request validation (detail array).",
    },
    500: PIPELINE_ERROR,
}
XACT_RESPONSES: dict[int | str, dict[str, Any]] = {
    **PROTECTED_ERRORS,
    200: {"description": "Synchronized IDs and per-item rejections, including mixed partial success."},
    422: {"model": ValidationErrorResponse, "description": "Invalid JSON/request types or batch exceeds 100 items."},
    500: PIPELINE_ERROR,
}

SNAPSHOT_DESCRIPTION = """Build the standalone source-preserving root ledger for offline PWA parsing.

`file_path` must be exactly the configured BEANCOUNT_FILE basename (for example
`main.bean`), never a client-selected included file. Includes/globs are resolved
server-side; unchanged source directives stay verbatim and Python plugin output
is materialized only under the snapshot export policy. Each request captures current
source files, including newly matched glob files; no snapshot cache or stale-success
fallback is used. Read or materialization failure is a request error. Exact `@@` total prices
are preserved in storage and snapshot, never converted to rounded `@` unit prices.
Build errors fail closed with `detail: {message, errors: [string]}`, never raw source.
Capture limits: 16 MiB per file, 64 MiB captured/expanded sources, 4096 files/include
visits, 64 include levels. Reads reject unsafe paths and symlinks. Only the configured
root is exposed; individual included/manual files and HTTP glob requests get 403.
"""
XACT_DESCRIPTION = """Synchronize up to 100 Beancount transaction strings; not a general mutation API.

Only Cashier-created completed `*` transactions with stable `cashier_id` metadata
and already-known server accounts are accepted. Incomplete `!` transactions,
include/plugin/option/open/close/balance/pad and all other directives are rejected.
Same cashier_id + same transaction is idempotent: synchronized without appending.
Same cashier_id + different transaction is rejected as a conflict, never overwritten.
Mixed batches have partial success: valid items commit; invalid items receive
concrete reasons in `rejected` at HTTP 200. A rejection may have null `cashier_id`
when an ID cannot be recovered. `synchronized` includes both new and existing IDs.

The only target is configured BEANCOUNT_MANUAL_TRANSACTIONS_FILE, normally
/workspace/manual_transactions.bean, the sole rw file in a read-only workspace.
No client target path or writable ledger directory is accepted. Under a file lock,
the server captures current files, rereads/deduplicates, builds a private candidate,
validates it through the configured full-root BEANCOUNT_FILE pipeline, then durably
appends accepted transactions to the configured file.
Exact `@@` total prices survive storage and snapshot without rounded `@` conversion.
Equivalent retries compare normalized transaction content, not literal whitespace;
differing total amount, currency or @/@@ notation is a conflict. Full-pipeline failure
can reject the entire pending new subset; HTTP 200 alone never means every item synced.
Limits per item: 64 KiB UTF-8, 256 postings, 64 metadata fields, metadata keys at most
64 characters and string values at most 1024 UTF-8 bytes. cashier_id is case-sensitive
ASCII, 1..128 characters, starts with a letter/digit, followed by letters/digits or
`.`, `_`, `:`, `-`. Violations are per-item rejections, not request-schema errors.
"""


def install_openapi_contract(app: FastAPI) -> None:
    """Extend FastAPI's cached schema without changing request/response execution."""
    generate_openapi = app.openapi

    def openapi() -> dict[str, Any]:
        schema = generate_openapi()
        schema["x-contract-version"] = CONTRACT_VERSION
        schema["x-cashier-limits"] = CASHIER_LIMITS
        return schema

    app.openapi = openapi
