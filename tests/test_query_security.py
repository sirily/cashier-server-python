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
from datetime import date
from decimal import Decimal
from pathlib import Path
from textwrap import dedent
from types import SimpleNamespace

import beanquery
import pytest
from beancount import loader
from beanquery.parser import ast

from cashier_snapshot import query as readonly

LEGACY_BALANCES_QUERY = "SELECT sum(number), currency, account ORDER BY account"


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


@pytest.mark.parametrize("bare", [False, True])
@pytest.mark.parametrize("query", [
    "SELECT account FROM #accounts ORDER BY account",
    "SELECT count(*) FROM #accounts",
    "SELECT count(*) FROM #transactions",
    "SELECT 1 FROM #transactions",
    "SELECT * FROM (SELECT account FROM #accounts WHERE account ~ 'Assets')",
    "SELECT * FROM (SELECT * FROM (SELECT payee FROM #transactions))",
    "SELECT account FROM #accounts WHERE account IN (SELECT account FROM #accounts)",
    "SELECT DISTINCT account FROM #accounts WHERE NOT account = 'other' ORDER BY account LIMIT 1",
    "SELECT date, meta['filename'] FROM #transactions WHERE year(date) BETWEEN 2020 AND 2030",
    "SELECT open.date, open.meta['filename'] FROM #accounts",
    "SELECT payee, sum(year(date)) FROM #transactions GROUP BY payee ORDER BY payee",
    "SELECT payee, flag, count(*) FROM #transactions GROUP BY payee, flag PIVOT BY payee, flag",
    "SELECT account FROM #accounts; BQL line comment",
    "SELECT 'CREATE TABLE and INSERT are only string data' FROM #transactions",
])
def test_readonly_queries_match_native_results(connection, query, bare):
    # Use native explicit table syntax as the reference for both spellings.
    expected = connection.execute(query).fetchall()
    if bare:
        query = query.replace("#accounts", "accounts").replace("#transactions", "transactions")
    assert readonly.execute_readonly(connection, query) == expected


@pytest.mark.parametrize("query", [
    LEGACY_BALANCES_QUERY,
    "select SUM(number), currency, account order by account asc",
    "\n SELECT\t sum ( number ) , currency , account\n ORDER BY account ASC \n",
    "SELECT /* balances */ sum(number), currency, account ORDER BY account",
    "SELECT sum(number), currency, account ORDER /* sort */ BY account ASC; BQL comment",
])
def test_legacy_balances_query_matches_native_postings(connection, query):
    statement = beanquery.parser.parse(query)
    assert statement.from_clause is None
    expected_statement = dataclasses.replace(
        beanquery.parser.parse(query), from_clause=ast.Table("postings")
    )

    readonly._validate_ast(statement)

    assert statement == expected_statement
    assert type(statement.from_clause) is ast.Table
    assert statement.from_clause.name == "postings"
    expected = connection.execute(LEGACY_BALANCES_QUERY).fetchall()
    assert expected == [
        (Decimal(5), "USD", "Assets:Cash"),
        (Decimal(-5), "USD", "Equity:Opening"),
    ]
    assert readonly.execute_readonly(connection, query) == expected


@pytest.mark.parametrize("query", [
    "SELECT number, currency, account ORDER BY account",
    "SELECT sum(units), currency, account ORDER BY account",
    "SELECT count(number), currency, account ORDER BY account",
    "SELECT abs(sum(number)), currency, account ORDER BY account",
    "SELECT sum(number, number), currency, account ORDER BY account",
    "SELECT sum(number), account, currency ORDER BY account",
    "SELECT sum(number), currency ORDER BY account",
    "SELECT sum(number), currency, account, payee ORDER BY account",
    "SELECT sum(number) AS total, currency, account ORDER BY account",
    "SELECT sum(number), currency AS code, account ORDER BY account",
    "SELECT sum(number), currency, account AS name ORDER BY account",
    "SELECT sum(number), currency, account",
    "SELECT sum(number), currency, account ORDER BY currency",
    "SELECT sum(number), currency, account ORDER BY account DESC",
    "SELECT sum(number), currency, account ORDER BY account, currency",
    "SELECT sum(number), currency, account ORDER BY 3",
    "SELECT sum(number), currency, account WHERE TRUE ORDER BY account",
    "SELECT sum(number), currency, account GROUP BY currency, account ORDER BY account",
    "SELECT sum(number), currency, account ORDER BY account PIVOT BY currency, account",
    "SELECT sum(number), currency, account ORDER BY account LIMIT 0",
    "SELECT sum(number), currency, account ORDER BY account LIMIT 1",
    "SELECT DISTINCT sum(number), currency, account ORDER BY account",
    "SELECT sum(number), currency, account FROM postings ORDER BY account",
    "SELECT sum(number), currency, account FROM #postings ORDER BY account",
    "SELECT sum(number), currency, account FROM # ORDER BY account",
    "SELECT sum(number), currency, account FROM TRUE ORDER BY account",
])
def test_legacy_balances_exception_rejects_altered_queries(connection, query):
    statement = beanquery.parser.parse(query)
    original_from = statement.from_clause
    with pytest.raises(readonly.QueryRejected):
        readonly._validate_ast(statement)
    assert statement.from_clause is original_from
    with pytest.raises(readonly.QueryRejected):
        readonly.execute_readonly(connection, query)


@pytest.mark.parametrize("template", [
    "SELECT * FROM ({query})",
    "SELECT * FROM (SELECT * FROM ({query}))",
    "SELECT 1 FROM #transactions WHERE 1 IN ({query})",
    "SELECT account FROM accounts WHERE account IN ({query})",
])
def test_legacy_balances_exception_is_top_level_only(connection, template):
    query = template.format(query=LEGACY_BALANCES_QUERY)
    statement = beanquery.parser.parse(query)
    original_from = statement.from_clause
    with pytest.raises(readonly.QueryRejected):
        readonly._validate_ast(statement)
    assert statement.from_clause is original_from
    with pytest.raises(readonly.QueryRejected):
        readonly.execute_readonly(connection, query)


@pytest.mark.parametrize(("resource_name", "limit", "accepted", "reason"), [
    ("MAX_AST_NODES", 10, True, "AST node limit"),
    ("MAX_AST_NODES", 9, False, "AST node limit"),
    ("MAX_AST_DEPTH", 4, True, "AST depth limit"),
    ("MAX_AST_DEPTH", 3, False, "AST depth limit"),
])
def test_legacy_source_rewrite_follows_full_resource_validation(
    monkeypatch, resource_name, limit, accepted, reason,
):
    # The original semantic AST has ten nodes and a maximum depth of four.
    statement = beanquery.parser.parse(LEGACY_BALANCES_QUERY)
    monkeypatch.setattr(readonly, resource_name, limit)
    if accepted:
        readonly._validate_ast(statement)
        assert type(statement.from_clause) is ast.Table
        assert statement.from_clause.name == "postings"
    else:
        with pytest.raises(readonly.QueryRejected, match=reason):
            readonly._validate_ast(statement)
        assert statement.from_clause is None


@pytest.mark.parametrize("location", ["sum_operand", "order_by"])
@pytest.mark.parametrize("node", [
    ast.Insert(ast.Table("postings"), None, [ast.Constant(1)]),
    dataclasses.make_dataclass("FutureNode", [], bases=(ast.Node,))(),
])
def test_legacy_source_is_not_rewritten_before_ast_allowlist_validation(location, node):
    statement = beanquery.parser.parse(LEGACY_BALANCES_QUERY)
    if location == "sum_operand":
        statement.targets[0].expression.operands[0] = node
    else:
        statement.order_by[0].column = node

    with pytest.raises(readonly.QueryRejected, match="forbidden AST node"):
        readonly._validate_ast(statement)

    assert statement.from_clause is None


def test_date_values_survive_worker_transfer(connection):
    result = readonly.execute_readonly(connection, "SELECT date FROM #transactions")
    assert isinstance(result, list)
    assert all(isinstance(row, tuple) for row in result)
    assert len(result) == 1
    assert type(result[0][0]) is date
    assert result[0][0] == date(2024, 1, 2)


@pytest.mark.parametrize("query", [
    'CREATE TABLE leak USING "csv:/etc/passwd"',
    'CREATE TABLE leak USING "beancount:/etc/passwd"',
    "CREATE TABLE leak AS SELECT account FROM #accounts",
    "INSERT INTO postings VALUES (1)",
    'SELECT * FROM (CREATE TABLE leak USING "csv:/etc/passwd")',
    "SELECT * FROM (INSERT INTO postings VALUES (1))",
    'SELECT account FROM #accounts;\nCREATE TABLE leak USING "csv:/etc/passwd"',
    "SELECT 1 FROM #transactions;\nSELECT 2 FROM #accounts",
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


@pytest.mark.parametrize("query", ["BALANCES", "JOURNAL", "PRINT"])
def test_rejects_old_readonly_forms(connection, query):
    with pytest.raises(readonly.QueryRejected, match="Only SELECT queries are allowed"):
        readonly._validate_ast(beanquery.parser.parse(query))
    with pytest.raises(readonly.QueryRejected, match="Only SELECT queries are allowed"):
        readonly.execute_readonly(connection, query)


@pytest.mark.parametrize("table", [
    "balances", "commodities", "documents", "entries", "events", "notes",
    "postings", "prices", "leak",
])
@pytest.mark.parametrize("prefix", ["", "#"])
@pytest.mark.parametrize("template", [
    "SELECT 1 FROM {source}",
    "SELECT * FROM (SELECT 1 FROM {source})",
    "SELECT account FROM #accounts WHERE account IN (SELECT account FROM {source})",
])
def test_rejects_other_tables_in_every_select(connection, table, prefix, template):
    query = template.format(source=prefix + table)
    if table == "balances" and not prefix:
        # BALANCES is a reserved token: exercise both its textual rejection and
        # the equivalent bare-table AST without relying on a parser error.
        statement = beanquery.parser.parse(template.format(source="accounts"))
        select = statement
        if template.startswith("SELECT * FROM ("):
            select = statement.from_clause
        elif "WHERE account IN" in template:
            select = statement.where_clause.right
        select.from_clause = ast.From(ast.Column(table))
    else:
        statement = beanquery.parser.parse(query)
    with pytest.raises(readonly.QueryRejected):
        readonly._validate_ast(statement)
    with pytest.raises(readonly.QueryRejected):
        readonly.execute_readonly(connection, query)


@pytest.mark.parametrize("source", [
    None, "#", "TRUE", "'Assets'", "account ~ 'Assets'", "accounts AND TRUE",
    "NOT accounts", "accounts OR transactions", "accounts = accounts",
    "grep('Assets', account)", "accounts.account", "accounts['name']",
    "accounts IN (SELECT account FROM #accounts)",
    "OPEN ON 2024-01-01", "CLOSE ON 2024-01-01", "CLEAR",
    "accounts OPEN ON 2024-01-01", "transactions OPEN ON 2024-01-01",
    "accounts CLOSE ON 2024-01-01", "transactions CLOSE",
    "accounts CLEAR", "transactions CLEAR",
])
@pytest.mark.parametrize("template", [
    "{query}",
    "SELECT * FROM ({query})",
    "SELECT 1 FROM #transactions WHERE 1 IN ({query})",
])
def test_rejects_implicit_sources_and_from_expression_bypasses(connection, source, template):
    inner = "SELECT 1" if source is None else f"SELECT 1 FROM {source}"
    query = template.format(query=inner)
    with pytest.raises(readonly.QueryRejected):
        readonly._validate_ast(beanquery.parser.parse(query))
    with pytest.raises(readonly.QueryRejected):
        readonly.execute_readonly(connection, query)


@pytest.mark.parametrize("query", [
    "SELECT 1 WHERE 1 IN (SELECT 1 FROM #transactions)",
    "SELECT 1 FROM #accounts WHERE 1 IN (SELECT 1 WHERE 1 IN (SELECT 1 FROM #transactions))",
])
def test_nested_explicit_source_does_not_authorize_implicit_select(connection, query):
    with pytest.raises(readonly.QueryRejected, match="explicit accounts or transactions source"):
        readonly._validate_ast(beanquery.parser.parse(query))
    with pytest.raises(readonly.QueryRejected, match="explicit accounts or transactions source"):
        readonly.execute_readonly(connection, query)


@pytest.mark.parametrize("table", [
    None, "", "balances", "commodities", "documents", "entries", "events",
    "notes", "postings", "prices", "leak",
])
@pytest.mark.parametrize("nested", [False, True])
def test_rejects_forbidden_ast_table_names(table, nested):
    statement = beanquery.parser.parse("SELECT 1 FROM #transactions")
    statement.from_clause = ast.Table(table)
    if nested:
        outer = beanquery.parser.parse("SELECT * FROM #accounts")
        outer.from_clause = statement
        statement = outer
    with pytest.raises(readonly.QueryRejected, match="only use accounts and transactions tables"):
        readonly._validate_ast(statement)


@pytest.mark.parametrize("table", ["accounts", "transactions"])
def test_bare_sources_are_canonicalized_in_every_select(table):
    statement = beanquery.parser.parse(
        f"SELECT * FROM (SELECT 1 FROM {table} "
        f"WHERE 1 IN (SELECT 1 FROM {table}))"
    )
    nested = statement.from_clause
    subquery = nested.where_clause.right
    assert type(nested.from_clause) is ast.From
    assert type(subquery.from_clause) is ast.From
    readonly._validate_ast(statement)
    for select in (nested, subquery):
        assert type(select.from_clause) is ast.Table
        assert select.from_clause.name == table


@pytest.mark.parametrize("expression", [
    ast.Table("accounts"),
    beanquery.parser.parse("SELECT 1 FROM #transactions"),
])
def test_from_expression_cannot_wrap_an_otherwise_allowed_source(expression):
    statement = beanquery.parser.parse("SELECT 1 FROM #accounts")
    statement.from_clause = ast.From(expression)
    with pytest.raises(readonly.QueryRejected):
        readonly._validate_ast(statement)


def test_bare_sources_are_not_canonicalized_before_full_ast_validation():
    statement = beanquery.parser.parse(
        "SELECT * FROM (SELECT 1 FROM accounts WHERE 1 IN (SELECT 1 FROM transactions))"
    )
    nested = statement.from_clause
    original_from = nested.from_clause
    subquery = nested.where_clause.right
    original_subquery_from = subquery.from_clause
    subquery.targets[0].expression = dataclasses.make_dataclass(
        "FutureNode", [], bases=(ast.Node,)
    )()
    with pytest.raises(readonly.QueryRejected, match="forbidden AST node"):
        readonly._validate_ast(statement)
    assert nested.from_clause is original_from
    assert subquery.from_clause is original_subquery_from


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
    beanquery.parser.parse("BALANCES"),
    beanquery.parser.parse("JOURNAL"),
    beanquery.parser.parse("PRINT"),
    dataclasses.make_dataclass("FutureNode", [], bases=(ast.Node,))(),
])
@pytest.mark.parametrize("location", ["target", "from", "subquery"])
def test_ast_allowlist_rejects_forbidden_nodes_at_any_depth(node, location):
    statement = beanquery.parser.parse("SELECT account FROM #accounts")
    if location == "target":
        statement.targets[0].expression = ast.Function("str", [node])
    elif location == "from":
        statement.from_clause = ast.From(node)
    else:
        nested = beanquery.parser.parse("SELECT account FROM #accounts")
        nested.where_clause = node
        statement.from_clause = nested
    with pytest.raises(readonly.QueryRejected, match="forbidden AST node"):
        readonly._validate_ast(statement)


def test_uses_only_trusted_dataset_not_parent_registry(connection):
    entries_table = connection.tables["entries"]
    trusted = SimpleNamespace(
        _cashier_entries=entries_table.entries,
        _cashier_options=entries_table.options,
        tables={name: object() for name in (None, "entries", "accounts", "transactions", "postings", "leak")},
        filename="/does/not/exist.bean",
    )
    assert readonly.execute_readonly(trusted, "SELECT account FROM #accounts") == [
        ("Assets:Cash",), ("Equity:Opening",),
    ]
    assert readonly.execute_readonly(trusted, "SELECT payee FROM #transactions") == [("Shop",)]
    assert readonly.execute_readonly(trusted, LEGACY_BALANCES_QUERY) == [
        (Decimal(5), "USD", "Assets:Cash"),
        (Decimal(-5), "USD", "Equity:Opening"),
    ]
    with pytest.raises(readonly.QueryRejected):
        readonly.execute_readonly(
            trusted, "SELECT sum(number), currency, account FROM #postings ORDER BY account"
        )
    with pytest.raises(readonly.QueryRejected):
        readonly.execute_readonly(trusted, "SELECT * FROM #leak")


def test_native_fallback_ignores_shadowed_tables(connection):
    connection.tables[None] = object()
    connection.tables["accounts"] = object()
    connection.tables["transactions"] = object()
    connection.tables["postings"] = object()
    assert readonly.execute_readonly(connection, "SELECT account FROM #accounts") == [
        ("Assets:Cash",), ("Equity:Opening",),
    ]
    assert readonly.execute_readonly(connection, "SELECT payee FROM #transactions") == [("Shop",)]
    assert readonly.execute_readonly(connection, LEGACY_BALANCES_QUERY) == [
        (Decimal(5), "USD", "Assets:Cash"),
        (Decimal(-5), "USD", "Equity:Opening"),
    ]


@pytest.mark.parametrize("connection", [
    SimpleNamespace(tables={}),
    SimpleNamespace(_cashier_entries=[], tables={}),
    SimpleNamespace(_cashier_entries="client data", _cashier_options={}),
])
def test_requires_preloaded_dataset(connection):
    with pytest.raises(readonly.QueryRejected, match="trusted preloaded"):
        readonly.execute_readonly(connection, "SELECT 1 FROM #transactions")


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
    assert readonly.execute_readonly(connection, "SELECT count(*) FROM #transactions") == [(1,)]
    assert not sentinel.exists()
    ledger.unlink()
    assert readonly.execute_readonly(connection, "SELECT count(*) FROM #transactions") == [(1,)]


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
    assert readonly.execute_readonly(connection, "SELECT count(*) FROM #transactions") == [(1,)]
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
    ("SELECT " + ", ".join("1" for _ in range(260)) + " FROM #transactions", "AST node"),
    ("SELECT " + "abs(" * 33 + "1" + ")" * 33 + " FROM #transactions", "AST depth|worker resource"),
])
def test_ast_resource_limits(connection, query, reason):
    with pytest.raises(readonly.QueryRejected, match=reason):
        readonly.execute_readonly(connection, query)


@pytest.mark.parametrize("source", ["#transactions", "transactions"])
@pytest.mark.parametrize("node_count", [512, 513])
def test_ast_node_limit_accepts_512_and_rejects_513(source, node_count):
    statement = beanquery.parser.parse(f"SELECT 1 FROM {source}")
    statement.targets = [ast.Target(ast.Constant(i), None) for i in range(254)]
    # SELECT + 508 target/constant nodes + FROM (one Table or From/Column).
    base_count = 510 if source.startswith("#") else 511
    expression = ast.Constant(True)
    for _ in range(node_count - base_count - 1):
        expression = ast.Not(expression)
    statement.where_clause = expression
    original_from = statement.from_clause
    if node_count == 512:
        readonly._validate_ast(statement)
        assert type(statement.from_clause) is ast.Table
        assert statement.from_clause.name == "transactions"
    else:
        with pytest.raises(readonly.QueryRejected, match="AST node limit"):
            readonly._validate_ast(statement)
        # A 513-node bare FROM must not be shrunk to 512 before the bounded walk.
        assert statement.from_clause is original_from


@pytest.mark.parametrize("source", ["#transactions", "transactions"])
def test_ast_depth_limit_accepts_32_and_rejects_33(source):
    statement = beanquery.parser.parse(f"SELECT 1 FROM {source}")
    expression = statement.targets[0].expression
    for _ in range(29):
        expression = ast.Neg(expression)
    statement.targets[0].expression = expression
    readonly._validate_ast(statement)
    statement.targets[0].expression = ast.Neg(expression)
    with pytest.raises(readonly.QueryRejected, match="AST depth limit"):
        readonly._validate_ast(statement)


@pytest.mark.parametrize("source", ["#transactions", "transactions"])
@pytest.mark.parametrize("depth", [32, 33])
def test_ast_source_depth_is_bounded_before_canonicalization(source, depth):
    statement = beanquery.parser.parse(f"SELECT * FROM {source}")
    original_from = statement.from_clause
    innermost = statement
    # A bare source contributes From + Column; a # source contributes Table.
    select_count = depth - (1 if source.startswith("#") else 2)
    for _ in range(select_count - 1):
        outer = beanquery.parser.parse("SELECT * FROM #transactions")
        outer.from_clause = statement
        statement = outer
    if depth == 32:
        readonly._validate_ast(statement)
        assert type(innermost.from_clause) is ast.Table
        assert innermost.from_clause.name == "transactions"
    else:
        with pytest.raises(readonly.QueryRejected, match="AST depth limit"):
            readonly._validate_ast(statement)
        assert innermost.from_clause is original_from


@pytest.mark.parametrize("character", ["x", "é"])
def test_query_accepts_exact_byte_limit(connection, character):
    prefix, suffix = "SELECT '", "' FROM #transactions"
    count, padding = divmod(readonly.MAX_QUERY_BYTES - len(prefix + suffix), len(character.encode()))
    value = character * count + "x" * padding
    query = prefix + value + suffix
    assert len(query.encode()) == readonly.MAX_QUERY_BYTES
    assert readonly.execute_readonly(connection, query) == [(value,)]


def test_pickle_looking_query_text_is_never_unpickled(connection, tmp_path):
    sentinel = tmp_path / "client-pickle-executed"
    payload = f"cos\nsystem\n(S'touch {sentinel}'\ntR."
    with pytest.raises(readonly.QueryRejected):
        readonly.execute_readonly(connection, payload)
    assert readonly.execute_readonly(connection, f'SELECT "{payload}" FROM #transactions') == [(payload,)]
    assert not sentinel.exists()


def test_row_limit_rejects_rather_than_silently_truncating(connection):
    entries = connection.tables["entries"].entries
    transaction = entries[-1]
    connection.tables["entries"].entries = entries[:2] + [transaction] * 10001
    assert readonly.execute_readonly(
        connection, "SELECT payee FROM #transactions LIMIT 10000"
    ) == [("Shop",)] * 10000
    with pytest.raises(readonly.QueryRejected, match="row limit"):
        readonly.execute_readonly(connection, "SELECT payee FROM #transactions")


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
        readonly.execute_readonly(connection, "SELECT 1 FROM #transactions")


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
        query = f"SELECT grep('(a+)+$', '{value}') FROM #transactions"
    else:
        query = "SELECT narration FROM #transactions WHERE narration ~ '(a+)+$'"
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
        readonly.execute_readonly(connection, "SELECT 1 FROM #transactions")
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
        readonly.execute_readonly(connection, "SELECT 1 FROM #transactions")
    assert workers[0].returncode == -signal.SIGKILL


@pytest.mark.parametrize(("query", "source", "expected"), [
    (f"SELECT 'cos system pickle data' FROM {prefix}{source}", source,
     [("cos system pickle data",)] * row_count)
    for source, row_count in [("accounts", 2), ("transactions", 1)]
    for prefix in ("", "#")
] + [
    (LEGACY_BALANCES_QUERY, "postings", [
        (Decimal(5), "USD", "Assets:Cash"),
        (Decimal(-5), "USD", "Equity:Opening"),
    ]),
])
def test_worker_applies_os_limits_and_treats_query_as_json(connection, monkeypatch, query, source, expected):
    table = connection.tables["entries"]

    # A string resembling pickle opcodes is still only a BQL string literal.
    request = json.dumps(query).encode() + b"\n" + pickle.dumps((table.entries, table.options))
    worker_tables = []
    original_execute = beanquery.Connection.execute

    def execute(worker_connection, statement, *args, **kwargs):
        worker_tables.append(set(worker_connection.tables))
        assert type(statement.from_clause) is ast.Table
        assert statement.from_clause.name == source
        return original_execute(worker_connection, statement, *args, **kwargs)

    monkeypatch.setattr(beanquery.Connection, "execute", execute)
    limits = []
    monkeypatch.setattr(resource, "setrlimit", lambda name, values: limits.append((name, values)))
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(request)))
    output = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", SimpleNamespace(buffer=output))
    readonly._worker()
    assert pickle.loads(output.getvalue()) == (True, expected)
    assert worker_tables == [{"accounts", "transactions", "postings"}]
    assert all(None not in tables for tables in worker_tables)
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
