"""Declarative OpenAPI metadata; response documentation does not re-encode ledger data."""

from typing import Any, Literal

from fastapi import Depends, FastAPI
from fastapi.security import HTTPBearer
from pydantic import BaseModel, Field, JsonValue, RootModel

from cashier_snapshot.http_limits import (
    BODY_TIMEOUT_SECONDS,
    MAX_QUERY_STRING_BYTES,
    MAX_REQUEST_BYTES,
)
from cashier_snapshot.query import (
    MAX_AST_DEPTH,
    MAX_AST_NODES,
    MAX_DATASET_BYTES,
    MAX_QUERY_BYTES,
    MAX_RESPONSE_BYTES,
    MAX_ROWS,
    MAX_WORKER_MEMORY_BYTES,
    QUERY_TIMEOUT_SECONDS,
)

CONTRACT_VERSION = "1.0.0"
SERVERS = [
    {"url": "/", "description": "Direct server paths (internal HTTP)"},
    {"url": "/api", "description": "External same-origin reverse proxy prefix"},
]
# Middleware remains authoritative; this non-rejecting dependency declares security only.
BEARER_DEPENDENCIES = [Depends(HTTPBearer(auto_error=False, scheme_name="CashierBearer"))]
CASHIER_LIMITS = {
    "request_body_bytes": MAX_REQUEST_BYTES,
    "query_string_bytes": MAX_QUERY_STRING_BYTES,
    "body_receive_seconds": BODY_TIMEOUT_SECONDS,
    "bql_query_bytes": MAX_QUERY_BYTES,
    "bql_ast_nodes": MAX_AST_NODES,
    "bql_ast_depth": MAX_AST_DEPTH,
    "bql_deadline_seconds": QUERY_TIMEOUT_SECONDS,
    "bql_cpu_seconds": 5,
    "bql_worker_memory_bytes": MAX_WORKER_MEMORY_BYTES,
    "bql_dataset_bytes": MAX_DATASET_BYTES,
    "bql_rows": MAX_ROWS,
    "bql_json_bytes": MAX_RESPONSE_BYTES,
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


class BqlValidationErrorResponse(BaseModel):
    detail: str | list[ValidationIssue]


class SnapshotBuildDetail(BaseModel):
    message: str
    errors: list[str]


class SnapshotBuildErrorResponse(BaseModel):
    detail: SnapshotBuildDetail | list[ValidationIssue]


class MissingQueryResponse(BaseModel):
    error: Literal["No query provided"]


class BqlRows(RootModel[list[list[JsonValue]]]):
    root: list[list[JsonValue]] = Field(max_length=MAX_ROWS)


class BqlResponse(RootModel[BqlRows | MissingQueryResponse]):
    """Arbitrary JSON array rows, or the historical missing/empty-query envelope."""


class SnapshotResponse(BaseModel):
    content: str = Field(description="Standalone source-preserving Beancount ledger text.")


class HealthResponse(BaseModel):
    ok: bool
    beancount_file_configured: bool
    beancount_loaded: bool


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
BQL_RESPONSES: dict[int | str, dict[str, Any]] = {
    **PROTECTED_ERRORS,
    200: {"model": BqlResponse, "description": "JSON array rows; absent/empty query returns an error envelope."},
    400: {
        "model": ErrorResponse,
        "description": "Invalid request, BQL syntax/source/AST, or BQL query/dataset/row/JSON size limit.",
    },
    422: {
        "model": BqlValidationErrorResponse,
        "description": "BQL execution/CPU deadline (string detail), or FastAPI request validation (detail array).",
    },
    500: PIPELINE_ERROR,
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
RELOAD_RESPONSES: dict[int | str, dict[str, Any]] = {
    **PROTECTED_ERRORS,
    200: {"description": "Reload completed; the response is JSON null.", "content": {"application/json": {"schema": {"type": "null"}}}},
    422: {"model": ErrorResponse, "description": "Beancount ledger could not be loaded safely."},
    500: PIPELINE_ERROR,
}

BQL_DESCRIPTION = """Execute read-only BQL against the preloaded server ledger.

The optional `query` parameter remains backward compatible: absent or empty text
returns HTTP 200 `{"error":"No query provided"}`. Results are arbitrary JSON array
rows, not objects keyed by column names; existing Decimal-to-JSON-number encoding
is preserved. At most 10,000 rows and 8 MiB JSON are returned.

Every SELECT, including nested SELECTs, needs an explicit `accounts` or
`transactions` source (`#accounts` / `#transactions` also work, as do subqueries
whose SELECTs have allowed explicit sources). The sole exception is one whole
top-level AST equivalent to `SELECT sum(number), currency, account ORDER BY account`:
it implicitly reads postings grouped by currency/account and returns rows
`[sum as JSON number, currency, account]`. No other implicit or explicit postings
access is allowed; wrapping or extending that special query is not allowed.

Current Sync forms include `SELECT account FROM #accounts WHERE close IS NULL ORDER BY account`
and `SELECT DISTINCT COALESCE(payee, narration) AS payee FROM transactions ORDER BY payee`.
Mutations, external sources, other tables, PRINT/JOURNAL/BALANCES, expression-based
sources and OPEN/CLOSE/CLEAR qualifiers are rejected. BQL is not a write channel.

Limits: 16 KiB UTF-8 query, AST 512 nodes/depth 32, 5 s wall deadline and 5 s CPU,
1 GiB worker memory, 64 MiB serialized dataset, four shared heavy-operation slots.
All requests have a 1 MiB body, 64 KiB query string and 15 s receive deadline;
Uvicorn admits at most 64 concurrent connections/tasks.
"""
SNAPSHOT_DESCRIPTION = """Build the standalone source-preserving root ledger for offline PWA parsing.

`file_path` must be exactly the configured BEANCOUNT_FILE basename (for example
`main.bean`), never a client-selected included file. Includes/globs are resolved
server-side; unchanged source directives stay verbatim and Python plugin output
is materialized only under the snapshot export policy. Exact `@@` total prices
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
the server rereads/deduplicates, builds a private candidate, validates it through
the configured full-root BEANCOUNT_FILE pipeline, then performs a crash-conscious
single-file write. This is not an atomic rename or a promise of crash atomicity.
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
