"""Read-only BQL over a preloaded, trusted Beancount dataset.

``execute_readonly(connection, query)`` accepts a native preloaded Beanquery
connection, or one with ``_cashier_entries`` and ``_cashier_options`` attributes.
Only entries/options are transferred: never the connection, tables, a DSN, or a
ledger filename. Each request gets fresh Beancount tables for Sync.
SELECT requires an explicit accounts/transactions source, except for the legacy
posting-totals query, which is bound to postings after bounded AST validation.

Parsing (including parser resource exhaustion), compilation (which can evaluate
constant regexes), execution, and result encoding happen in an exec'd Python
subprocess. The caller must bound concurrent invocations and run this synchronous
API off the event loop. A deadline kills and reaps the subprocess, not merely the
thread waiting for it. No application entry point is imported, and no Python
``preexec_fn`` or multiprocessing fork of a threaded server is used.

Pickle is used ONLY on anonymous pipes owned by this parent and its worker, for
trusted loader-created Beancount objects and locally generated results. Never
supply client-provided pickle bytes, cached pickle files, or untrusted Python
objects as entries/options. BQL text is parsed as data, not unpickled. This is an
AST/resource boundary, not an OS filesystem or network sandbox.
"""

from __future__ import annotations

import dataclasses
import io
import json
import os
import pickle
import selectors
import signal
import subprocess
import sys
import time
from datetime import date
from decimal import Decimal
from pathlib import Path

MAX_QUERY_BYTES = 16 * 1024
MAX_AST_NODES = 512
MAX_AST_DEPTH = 32
MAX_ROWS = 10_000
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_DATASET_BYTES = 64 * 1024 * 1024
MAX_WORKER_MEMORY_BYTES = 1024 * 1024 * 1024
QUERY_TIMEOUT_SECONDS = 5.0

# Explicitly review new sources on dependency upgrades rather than trusting the
# mutable connection.tables registry, including tables shadowing these names.
_READONLY_TABLES = frozenset({"accounts", "transactions"})
_POSTING_TOTALS_QUERY = "SELECT sum(number), currency, account ORDER BY account"
_WORKER_TABLES = _READONLY_TABLES | {"postings"}
_READONLY_NODES = frozenset({
    "Select", "Target", "Asterisk", "From",
    "GroupBy", "OrderBy", "PivotBy", "Table", "Column", "Function", "Attribute",
    "Subscript", "Constant", "Between", "Not", "IsNull", "IsNotNull", "And",
    "Or", "Equal", "NotEqual", "Greater", "GreaterEq", "Less", "LessEq",
    "Match", "NotMatch", "Matches", "In", "NotIn", "Neg", "Mul", "Div", "Mod",
    "Add", "Sub", "Any", "All",
})


class QueryRejected(ValueError):
    """The query is invalid, not read-only, or exceeds a size/resource limit."""


class QueryTimeout(TimeoutError):
    """The query worker exceeded its execution deadline and was reaped."""


class _BoundedBuffer(io.BytesIO):
    def __init__(self, limit: int, label: str):
        super().__init__()
        self.limit = limit
        self.label = label

    def write(self, data):
        if self.tell() + len(data) > self.limit:
            raise QueryRejected(f"Query {self.label} exceeds byte limit")
        return super().write(data)


def _serialize(value, limit: int, label: str) -> bytes:
    with _BoundedBuffer(limit, label) as buffer:
        pickle.Pickler(buffer, protocol=pickle.HIGHEST_PROTOCOL).dump(value)
        return buffer.getvalue()


def _check_query(query: str) -> None:
    if not isinstance(query, str) or not query.strip():
        raise QueryRejected("Query must be nonempty BQL text")
    if len(query) > MAX_QUERY_BYTES:
        raise QueryRejected("Query exceeds byte limit")
    try:
        size = len(query.encode("utf-8"))
    except UnicodeError as exc:
        raise QueryRejected("Query must be valid UTF-8 text") from exc
    if size > MAX_QUERY_BYTES:
        raise QueryRejected("Query exceeds byte limit")


def _validate_ast(statement) -> None:
    from beanquery.parser import ast, parse

    if type(statement) is not ast.Select:
        raise QueryRejected("Only SELECT queries are allowed")
    allowed = {getattr(ast, name) for name in _READONLY_NODES}
    pending = [(statement, 1)]
    count = 0
    selects = []
    while pending:
        node, depth = pending.pop()
        if isinstance(node, ast.Node):
            count += 1
            if count > MAX_AST_NODES:
                raise QueryRejected("Query exceeds AST node limit")
            if depth > MAX_AST_DEPTH:
                raise QueryRejected("Query exceeds AST depth limit")
            # Exact classes fail closed on future nodes, including mutations
            # hidden in subqueries, function operands, or FROM expressions.
            if type(node) not in allowed:
                raise QueryRejected("Query contains a forbidden AST node")
            if type(node) is ast.Table and getattr(node, "name", None) not in _READONLY_TABLES:
                raise QueryRejected("Query may only use accounts and transactions tables")
            if type(node) is ast.Select:
                selects.append(node)
            if not dataclasses.is_dataclass(node):
                raise QueryRejected("Query contains an unsupported AST node")
            pending.extend(
                (getattr(node, field.name), depth + 1)
                for field in dataclasses.fields(node)
                if field.name != "parseinfo"
            )
        elif isinstance(node, (list, tuple)):
            pending.extend((child, depth) for child in node)
        elif not isinstance(node, (str, int, bool, type(None), date, Decimal)):
            raise QueryRejected("Query contains an unsupported AST value")

    # Preserve only the original Sync totals shape, not general implicit
    # postings access. Dataclass equality ignores parser source locations.
    posting_totals = statement == parse(_POSTING_TOTALS_QUERY)
    source_rewrites = []
    for select in selects:
        source = select.from_clause
        if select is statement and posting_totals:
            source_rewrites.append((select, "postings"))
            continue
        if type(source) in {ast.Table, ast.Select}:
            continue
        if type(source) is ast.From and type(source.expression) is ast.Column:
            name = getattr(source.expression, "name", None)
            if name not in _READONLY_TABLES:
                raise QueryRejected("Query may only use accounts and transactions tables")
            if source.open is None and source.close is None and source.clear is None:
                source_rewrites.append((select, name))
                continue
        raise QueryRejected("Query requires an explicit accounts or transactions source")

    # Bare FROM names are parsed as expressions. Resolve them explicitly rather
    # than retaining Beanquery's implicit postings table or column-name fallback.
    # Do this only after the complete AST passed the bounded validation above.
    for select, name in source_rewrites:
        select.from_clause = ast.Table(name)


def _dataset(connection):
    missing = object()
    entries = getattr(connection, "_cashier_entries", missing)
    options = getattr(connection, "_cashier_options", missing)
    if entries is missing and options is missing:
        # Compatibility for native connections only: do not serialize table
        # objects or carry a caller's custom sources/functions into the worker.
        from beanquery.sources.beancount import EntriesTable

        table = getattr(connection, "tables", {}).get("entries")
        if type(table) is not EntriesTable:
            raise QueryRejected("A trusted preloaded Beancount dataset is required")
        entries, options = table.entries, table.options
    if not isinstance(entries, (list, tuple)) or not isinstance(options, dict):
        raise QueryRejected("A trusted preloaded Beancount dataset is required")
    return entries, options


def _communicate_bounded(process, request: bytes, deadline: float) -> bytes:
    """Pump both pipes without blocking on input or buffering unbounded output."""
    response = bytearray()
    offset = 0
    data = memoryview(request)
    with selectors.DefaultSelector() as selector:
        for stream, event in (
            (process.stdin, selectors.EVENT_WRITE),
            (process.stdout, selectors.EVENT_READ),
        ):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, event, stream)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise QueryTimeout("Query execution timed out")
            events = selector.select(remaining)
            if not events:
                raise QueryTimeout("Query execution timed out")
            for key, _ in events:
                stream = key.data
                if stream is process.stdin:
                    try:
                        offset += os.write(stream.fileno(), data[offset:offset + 65536])
                    except BrokenPipeError:
                        offset = len(data)
                    except BlockingIOError:
                        continue
                    if offset == len(data):
                        selector.unregister(stream)
                        stream.close()
                else:
                    try:
                        chunk = os.read(
                            stream.fileno(),
                            min(65536, MAX_RESPONSE_BYTES + 1 - len(response)),
                        )
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(stream)
                        stream.close()
                    response.extend(chunk)
                    if len(response) > MAX_RESPONSE_BYTES:
                        raise QueryRejected("Query response exceeds byte limit")
    try:
        process.wait(timeout=max(0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired as exc:
        raise QueryTimeout("Query execution timed out") from exc
    return bytes(response)


def execute_readonly(connection, query: str) -> list[tuple]:
    """Execute bounded read-only BQL; callers must limit concurrent workers."""
    _check_query(query)
    dataset = _serialize(_dataset(connection), MAX_DATASET_BYTES, "dataset")
    # The client supplies only JSON text, never the bytes passed to unpickling.
    request = json.dumps(query).encode("ascii") + b"\n" + dataset
    deadline = time.monotonic() + QUERY_TIMEOUT_SECONDS
    process = subprocess.Popen(
        [sys.executable, "-I", str(Path(__file__).resolve())],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=0,
        close_fds=True,
        cwd="/",
        env={"PATH": os.defpath, "LANG": "C.UTF-8"},
    )
    try:
        response = _communicate_bounded(process, request, deadline)
        if process.returncode == -signal.SIGXCPU:
            raise QueryTimeout("Query exceeded CPU time limit")
        if process.returncode != 0 or not response:
            raise QueryRejected("Query worker failed or exceeded resource limits")
        try:
            successful, result = pickle.loads(response)
        except Exception as exc:
            raise QueryRejected("Invalid query worker response") from exc
        if not successful:
            raise QueryRejected(result)
        if not isinstance(result, list) or any(not isinstance(row, tuple) for row in result):
            raise QueryRejected("Invalid query worker rows")
        if len(result) > MAX_ROWS:
            raise QueryRejected("Query exceeds row limit")
        return result
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                stream.close()


def _check_http_json_size(rows) -> None:
    """Bound the actual FastAPI JSON shape, not pickle's memoized wire size."""
    from beancount.core.inventory import Inventory
    from fastapi.encoders import jsonable_encoder

    # Match main.beancount's Inventory conversion and JSONResponse formatting.
    # Encoding stays inside the resource-limited worker, including allocations
    # made by jsonable_encoder for nested Beancount values.
    converted = [
        tuple(list(value) if isinstance(value, Inventory) else value for value in row)
        for row in rows
    ]
    encoded = jsonable_encoder(converted)
    encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    size = 0
    for chunk in encoder.iterencode(encoded):
        size += len(chunk.encode("utf-8"))
        if size > MAX_RESPONSE_BYTES:
            raise QueryRejected("Query HTTP JSON response exceeds byte limit")


def _worker() -> None:
    # Applied after exec, never in preexec_fn in the threaded application.
    import resource

    resource.setrlimit(resource.RLIMIT_AS, (MAX_WORKER_MEMORY_BYTES, MAX_WORKER_MEMORY_BYTES))
    resource.setrlimit(resource.RLIMIT_CPU, (5, 5))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    try:
        import beanquery

        header = sys.stdin.buffer.readline(MAX_QUERY_BYTES * 6 + 3)
        if not header.endswith(b"\n"):
            raise QueryRejected("Invalid query request")
        query = json.loads(header)
        _check_query(query)
        statement = beanquery.parser.parse(query)
        _validate_ast(statement)
        dataset = sys.stdin.buffer.read(MAX_DATASET_BYTES + 1)
        if len(dataset) > MAX_DATASET_BYTES:
            raise QueryRejected("Query dataset exceeds byte limit")
        entries, options = pickle.loads(dataset)
        connection = beanquery.connect("beancount:", entries=entries, errors=[], options=options)
        for name in list(connection.tables):
            if name not in _WORKER_TABLES:
                del connection.tables[name]
        rows = connection.execute(statement).fetchall()
        if len(rows) > MAX_ROWS:
            raise QueryRejected("Query exceeds row limit")
        _check_http_json_size(rows)
        response = _serialize((True, rows), MAX_RESPONSE_BYTES, "response")
    except (MemoryError, RecursionError):
        response = _serialize((False, "Query exceeded worker resource limits"), MAX_RESPONSE_BYTES, "response")
    except QueryRejected as exc:
        response = _serialize((False, str(exc)[:1024]), MAX_RESPONSE_BYTES, "response")
    except Exception:  # noqa: BLE001 -- isolate all parser/compiler errors; disclose no internals.
        response = _serialize((False, "Invalid BQL query"), MAX_RESPONSE_BYTES, "response")
    sys.stdout.buffer.write(response)
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    _worker()
