"""The BQL boundary must reject mutations and contain expensive read queries."""

import dataclasses
import io
import json
import os
import pickle
import resource
import signal
import subprocess
import sys
import time
from pathlib import Path
from textwrap import dedent
from types import SimpleNamespace

import beanquery
import pytest
from beancount import loader
from beancount.core.inventory import Inventory
from beanquery.parser import ast

from cashier_snapshot import query as readonly


@pytest.fixture
def connection():
    entries, errors, options = loader.load_string(dedent('''
        2024-01-01 open Assets:Cash USD
        2024-01-01 open Equity:Opening USD
        2024-01-02 * "Shop" "Lunch"
          Assets:Cash       5 USD
          Equity:Opening   -5 USD
    '''))
    assert not errors
    return beanquery.connect("beancount:", entries=entries, errors=[], options=options)


@pytest.mark.parametrize("query", [
    "SELECT account, sum(position) GROUP BY account ORDER BY account",
    "BALANCES",
    "JOURNAL",
    "PRINT",
    "SELECT account FROM #accounts ORDER BY account",
    "SELECT 1 FROM #",
    "SELECT * FROM (SELECT account WHERE account ~ 'Assets')",
    "SELECT account WHERE account IN (SELECT account FROM #accounts)",
    "SELECT DISTINCT account WHERE NOT account = 'other' ORDER BY account LIMIT 1",
    "SELECT entry.narration, meta['filename'] WHERE number BETWEEN -10 AND 10",
    "SELECT account, currency, sum(position) GROUP BY account, currency PIVOT BY account, currency",
    "SELECT account; BQL line comment",
    "SELECT 'CREATE TABLE and INSERT are only string data' FROM #",
])
def test_readonly_queries_match_native_results(connection, query):
    expected = connection.execute(query).fetchall()
    assert readonly.execute_readonly(connection, query) == expected


def test_inventory_values_survive_worker_transfer(connection):
    result = readonly.execute_readonly(connection, "BALANCES")
    assert isinstance(result, list)
    assert all(isinstance(row, tuple) for row in result)
    assert isinstance(result[0][1], Inventory)
    assert str(result[0][1]) == "(5 USD)"


@pytest.mark.parametrize("query", [
    'CREATE TABLE leak USING "csv:/etc/passwd"',
    'CREATE TABLE leak USING "beancount:/etc/passwd"',
    "CREATE TABLE leak AS SELECT account",
    "INSERT INTO postings VALUES (1)",
    'SELECT * FROM (CREATE TABLE leak USING "csv:/etc/passwd")',
    "SELECT * FROM (INSERT INTO postings VALUES (1))",
    'SELECT account;\nCREATE TABLE leak USING "csv:/etc/passwd"',
    "SELECT 1;\nSELECT 2",
    "SELECT * FROM #leak",
    "SELECT * FROM (SELECT * FROM #leak)",
    "DELETE FROM postings",
    "ATTACH 'csv:/etc/passwd'",
])
def test_rejects_mutations_external_sources_and_multiple_statements(connection, query):
    before = dict(connection.tables)
    with pytest.raises(readonly.QueryRejected):
        readonly.execute_readonly(connection, query)
    assert connection.tables == before


def test_rejects_csv_source_before_opening_it(connection, tmp_path):
    # If compilation reaches CSV source creation, opening this FIFO blocks.
    fifo = tmp_path / "secret.csv"
    os.mkfifo(fifo)
    with pytest.raises(readonly.QueryRejected, match="Only SELECT"):
        readonly.execute_readonly(connection, f'CREATE TABLE leak USING "csv:{fifo}"')
    assert not (tmp_path / "output.csv").exists()
    with pytest.raises(readonly.QueryRejected, match="Only SELECT"):
        readonly.execute_readonly(
            connection, f'CREATE TABLE leak (account str) USING "csv:{tmp_path / "output.csv"}"'
        )
    assert not (tmp_path / "output.csv").exists()


@pytest.mark.parametrize("node", [
    ast.CreateTable("leak", None, "csv:/etc/passwd", None),
    ast.Insert(ast.Table("postings"), None, [ast.Constant(1)]),
    dataclasses.make_dataclass("FutureNode", [], bases=(ast.Node,))(),
])
@pytest.mark.parametrize("location", ["target", "from", "subquery"])
def test_ast_allowlist_rejects_forbidden_nodes_at_any_depth(node, location):
    statement = beanquery.parser.parse("SELECT account")
    if location == "target":
        statement.targets[0].expression = ast.Function("str", [node])
    elif location == "from":
        statement.from_clause = ast.From(node)
    else:
        nested = beanquery.parser.parse("SELECT account")
        nested.where_clause = node
        statement.from_clause = nested
    with pytest.raises(readonly.QueryRejected, match="forbidden AST node"):
        readonly._validate_ast(statement)


def test_uses_only_trusted_dataset_not_parent_registry(connection):
    entries_table = connection.tables["entries"]
    trusted = SimpleNamespace(
        _cashier_entries=entries_table.entries,
        _cashier_options=entries_table.options,
        tables={name: object() for name in (None, "entries", "accounts", "leak")},
        filename="/does/not/exist.bean",
    )
    assert readonly.execute_readonly(trusted, "SELECT account FROM #accounts") == [
        ("Assets:Cash",), ("Equity:Opening",),
    ]
    with pytest.raises(readonly.QueryRejected):
        readonly.execute_readonly(trusted, "SELECT * FROM #leak")


def test_native_fallback_ignores_shadowed_tables(connection):
    connection.tables[None] = object()
    connection.tables["accounts"] = object()
    assert readonly.execute_readonly(connection, "SELECT account FROM #accounts") == [
        ("Assets:Cash",), ("Equity:Opening",),
    ]


@pytest.mark.parametrize("connection", [
    SimpleNamespace(tables={}),
    SimpleNamespace(_cashier_entries=[], tables={}),
    SimpleNamespace(_cashier_entries="client data", _cashier_options={}),
])
def test_requires_preloaded_dataset(connection):
    with pytest.raises(readonly.QueryRejected, match="trusted preloaded"):
        readonly.execute_readonly(connection, "SELECT 1")


def test_worker_does_not_load_ledger_files_or_plugins(connection, tmp_path):
    sentinel = tmp_path / "plugin-ran"
    plugin = tmp_path / "untrusted_plugin.py"
    plugin.write_text(f"from pathlib import Path\nPath({str(sentinel)!r}).touch()\n")
    ledger = tmp_path / "ledger.bean"
    ledger.write_text('plugin "untrusted_plugin"\n')
    table = connection.tables["entries"]
    table.options["filename"] = str(ledger)
    table.options["plugin"] = [("untrusted_plugin", None)]
    for entry in table.entries:
        entry.meta["filename"] = str(ledger)
    assert readonly.execute_readonly(connection, "SELECT count(*)") == [(2,)]
    assert not sentinel.exists()
    ledger.unlink()
    assert readonly.execute_readonly(connection, "SELECT count(*)") == [(2,)]


def test_exec_worker_is_isolated_and_strips_secrets(connection, monkeypatch, tmp_path):
    sentinel = tmp_path / "imported"
    source = f"from pathlib import Path\nPath({str(sentinel)!r}).touch()\nraise RuntimeError('injected')\n"
    for name in ("beanquery.py", "sitecustomize.py", "main.py"):
        (tmp_path / name).write_text(source)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    monkeypatch.setenv("CASHIER_API_TOKEN", "must-not-reach-worker")
    calls = []
    original = subprocess.Popen

    def record(*args, **kwargs):
        process = original(*args, **kwargs)
        calls.append((args, kwargs, process))
        return process

    monkeypatch.setattr(readonly.subprocess, "Popen", record)
    assert readonly.execute_readonly(connection, "SELECT count(*)") == [(2,)]
    assert not sentinel.exists()
    args, kwargs, process = calls[0]
    assert args[0][:2] == [sys.executable, "-I"]
    assert Path(args[0][2]).is_absolute()
    assert kwargs["cwd"] == "/"
    assert kwargs["close_fds"] is True
    assert kwargs["stderr"] == subprocess.DEVNULL
    assert "preexec_fn" not in kwargs
    assert kwargs["env"] == {"PATH": os.defpath, "LANG": "C.UTF-8"}
    assert process.returncode == 0


@pytest.mark.parametrize("query", [None, b"SELECT 1", "", "  ", "\ud800", "x" * 16385, "é" * 8193])
def test_query_text_limit_is_checked_before_spawning(connection, monkeypatch, query):
    def forbidden(*args, **kwargs):
        pytest.fail("Invalid input must not spawn a worker")

    monkeypatch.setattr(readonly.subprocess, "Popen", forbidden)
    with pytest.raises(readonly.QueryRejected):
        readonly.execute_readonly(connection, query)


@pytest.mark.parametrize(("query", "reason"), [
    ("SELECT " + ", ".join("1" for _ in range(260)), "AST node"),
    ("SELECT " + "abs(" * 33 + "1" + ")" * 33, "AST depth|worker resource"),
])
def test_ast_resource_limits(connection, query, reason):
    with pytest.raises(readonly.QueryRejected, match=reason):
        readonly.execute_readonly(connection, query)


def test_ast_node_limit_accepts_512_and_rejects_513():
    statement = beanquery.parser.parse("SELECT 1")
    statement.targets = [ast.Target(ast.Constant(i), None) for i in range(255)]
    statement.where_clause = ast.Constant(True)
    readonly._validate_ast(statement)
    statement.where_clause = ast.Not(statement.where_clause)
    with pytest.raises(readonly.QueryRejected, match="AST node limit"):
        readonly._validate_ast(statement)


def test_ast_depth_limit_accepts_32_and_rejects_33():
    statement = beanquery.parser.parse("SELECT 1")
    expression = statement.targets[0].expression
    for _ in range(29):
        expression = ast.Neg(expression)
    statement.targets[0].expression = expression
    readonly._validate_ast(statement)
    statement.targets[0].expression = ast.Neg(expression)
    with pytest.raises(readonly.QueryRejected, match="AST depth limit"):
        readonly._validate_ast(statement)


@pytest.mark.parametrize("character", ["x", "é"])
def test_query_accepts_exact_byte_limit(connection, character):
    prefix, suffix = "SELECT '", "' FROM #"
    count = (readonly.MAX_QUERY_BYTES - len(prefix + suffix)) // len(character.encode())
    value = character * count
    query = prefix + value + suffix
    assert len(query.encode()) == readonly.MAX_QUERY_BYTES
    assert readonly.execute_readonly(connection, query) == [(value,)]


def test_pickle_looking_query_text_is_never_unpickled(connection, tmp_path):
    sentinel = tmp_path / "client-pickle-executed"
    payload = f"cos\nsystem\n(S'touch {sentinel}'\ntR."
    with pytest.raises(readonly.QueryRejected):
        readonly.execute_readonly(connection, payload)
    assert readonly.execute_readonly(connection, f'SELECT "{payload}" FROM #') == [(payload,)]
    assert not sentinel.exists()


def test_row_limit_rejects_rather_than_silently_truncating(connection):
    entries = connection.tables["entries"].entries
    transaction = entries[-1]
    connection.tables["entries"].entries = entries[:2] + [transaction] * 5001
    assert len(readonly.execute_readonly(connection, "SELECT account LIMIT 10000")) == 10000
    with pytest.raises(readonly.QueryRejected, match="row limit"):
        readonly.execute_readonly(connection, "SELECT account")


def test_response_byte_limit_in_worker(connection):
    entries = connection.tables["entries"].entries
    entries[-1] = entries[-1]._replace(narration="x" * readonly.MAX_RESPONSE_BYTES)
    with pytest.raises(readonly.QueryRejected, match="response exceeds byte limit"):
        readonly.execute_readonly(connection, "SELECT narration FROM #transactions")


def test_dataset_byte_limit_before_spawning(connection, monkeypatch):
    monkeypatch.setattr(readonly, "MAX_DATASET_BYTES", 128)

    def forbidden(*args, **kwargs):
        pytest.fail("Oversized dataset must not spawn a worker")

    monkeypatch.setattr(readonly.subprocess, "Popen", forbidden)
    with pytest.raises(readonly.QueryRejected, match="dataset exceeds byte limit"):
        readonly.execute_readonly(connection, "SELECT 1")


def test_bounded_pickle_never_writes_over_limit():
    buffer = readonly._BoundedBuffer(8, "test")
    buffer.write(b"12345678")
    with pytest.raises(readonly.QueryRejected, match="byte limit"):
        buffer.write(b"9")
    assert buffer.getvalue() == b"12345678"


@pytest.mark.parametrize("constant", [False, True])
def test_regex_timeout_kills_and_reaps_worker(connection, monkeypatch, constant):
    value = "a" * 4096 + "!"
    entries = connection.tables["entries"].entries
    entries[-1] = entries[-1]._replace(narration=value)
    if constant:
        query = f"SELECT grep('(a+)+$', '{value}') FROM #"
    else:
        query = "SELECT narration WHERE narration ~ '(a+)+$'"
    workers = []
    original = subprocess.Popen

    def record(*args, **kwargs):
        process = original(*args, **kwargs)
        workers.append(process)
        return process

    monkeypatch.setattr(readonly.subprocess, "Popen", record)
    monkeypatch.setattr(readonly, "QUERY_TIMEOUT_SECONDS", 0.8)
    started = time.monotonic()
    with pytest.raises(readonly.QueryTimeout):
        readonly.execute_readonly(connection, query)
    assert time.monotonic() - started < 4
    assert len(workers) == 1
    process = workers[0]
    assert process.returncode == -signal.SIGKILL
    assert process.stdin.closed and process.stdout.closed
    with pytest.raises(ChildProcessError):
        os.waitpid(process.pid, os.WNOHANG)


def test_parent_bounds_output_even_if_worker_misbehaves(connection, monkeypatch):
    workers = []
    original = subprocess.Popen

    def noisy_worker(*args, **kwargs):
        process = original(
            [sys.executable, "-I", "-c", "import os; os.write(1, b'x' * 1048576)"],
            **kwargs,
        )
        workers.append(process)
        return process

    monkeypatch.setattr(readonly, "MAX_RESPONSE_BYTES", 1024)
    monkeypatch.setattr(readonly.subprocess, "Popen", noisy_worker)
    with pytest.raises(readonly.QueryRejected, match="response exceeds byte limit"):
        readonly.execute_readonly(connection, "SELECT 1")
    assert workers[0].poll() is not None
    assert workers[0].stdin.closed and workers[0].stdout.closed


def test_timeout_includes_blocked_input_pipe(connection, monkeypatch):
    entries = connection.tables["entries"].entries
    entries[-1] = entries[-1]._replace(narration="x" * 1048576)
    workers = []
    original = subprocess.Popen

    def stalled_worker(*args, **kwargs):
        process = original([sys.executable, "-I", "-c", "import time; time.sleep(30)"], **kwargs)
        workers.append(process)
        return process

    monkeypatch.setattr(readonly, "QUERY_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(readonly.subprocess, "Popen", stalled_worker)
    with pytest.raises(readonly.QueryTimeout):
        readonly.execute_readonly(connection, "SELECT 1")
    assert workers[0].returncode == -signal.SIGKILL


def test_worker_applies_os_limits_and_treats_query_as_json(connection, monkeypatch):
    table = connection.tables["entries"]

    # A string resembling pickle opcodes is still only a BQL string literal.
    query = "SELECT 'cos system pickle data' FROM #"
    request = json.dumps(query).encode() + b"\n" + pickle.dumps((table.entries, table.options))
    limits = []
    monkeypatch.setattr(resource, "setrlimit", lambda name, values: limits.append((name, values)))
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(request)))
    output = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", SimpleNamespace(buffer=output))
    readonly._worker()
    assert pickle.loads(output.getvalue()) == (True, [("cos system pickle data",)])
    assert (resource.RLIMIT_AS, (1024 * 1024 * 1024,) * 2) in limits
    assert (resource.RLIMIT_CPU, (5, 5)) in limits
    assert (resource.RLIMIT_CORE, (0, 0)) in limits


def test_http_json_limit_does_not_trust_pickle_memoization(monkeypatch):
    rows = [("x" * 200,)] * 12
    assert len(pickle.dumps((True, rows))) < 1024
    assert len(json.dumps(rows, separators=(",", ":")).encode()) > 1024
    monkeypatch.setattr(readonly, "MAX_RESPONSE_BYTES", 1024)
    with pytest.raises(readonly.QueryRejected, match="HTTP JSON response exceeds"):
        readonly._check_http_json_size(rows)


@pytest.mark.parametrize("value", ["é", "\\n", '"', "\\\\"])
def test_http_json_limit_counts_utf8_and_escaping(monkeypatch, value):
    rows = [(value * 20,)]
    encoded = json.dumps(rows, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
    monkeypatch.setattr(readonly, "MAX_RESPONSE_BYTES", len(encoded))
    readonly._check_http_json_size(rows)
    monkeypatch.setattr(readonly, "MAX_RESPONSE_BYTES", len(encoded) - 1)
    with pytest.raises(readonly.QueryRejected, match="HTTP JSON response exceeds"):
        readonly._check_http_json_size(rows)


def test_http_json_limit_checked_by_real_worker(connection):
    entries = connection.tables["entries"].entries
    transaction = entries[-1]._replace(narration="x" * 1024)
    connection.tables["entries"].entries = entries[:2] + [transaction] * 9000
    assert len(pickle.dumps((entries[:2] + [transaction] * 9000, connection.tables["entries"].options))) < 64 * 1024
    with pytest.raises(readonly.QueryRejected, match="HTTP JSON response exceeds"):
        readonly.execute_readonly(connection, "SELECT narration FROM #transactions")


def test_exceptions_match_public_contract():
    assert issubclass(readonly.QueryRejected, ValueError)
    assert issubclass(readonly.QueryTimeout, TimeoutError)
