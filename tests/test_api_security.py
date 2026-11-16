"""HTTP regression coverage for authentication, BQL and resource boundaries."""

import asyncio
import json
import threading
from pathlib import Path

import anyio
import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

import main
from cashier_snapshot import http_limits


@pytest.fixture
def api(monkeypatch, tmp_path):
    root = tmp_path / "main.bean"
    root.write_text(
        '2024-01-01 open Assets:Cash USD\n'
        '2024-01-01 open Expenses:Food USD\n'
        '2024-01-02 * "Shop" "Lunch"\n'
        '  Assets:Cash -5 USD\n'
        '  Expenses:Food 5 USD\n', encoding="utf-8",
    )
    monkeypatch.setattr(main, "CASHIER_API_TOKEN", "test-api-token")
    monkeypatch.setattr(main, "BEAN_FILE", str(root))
    monkeypatch.setattr(main.app.state, "connection", main.preload_beancount_data(), raising=False)
    return TestClient(main.app, headers={"Authorization": "Bearer test-api-token"})


@pytest.mark.parametrize("host", [
    "testserver/health?", "testserver/health#", "testserver/health", "user@testserver",
    "testserver\\health", "testserver health", "", "testserver:bad",
])
@pytest.mark.parametrize("path,method", [("/ping", "GET"), ("/infrastructure?file_path=main.bean", "GET"), ("/xact", "POST")])
def test_malformed_hosts_cannot_exempt_protected_routes(api, host, path, method):
    response = api.request(method, path, headers={"Host": host, "Authorization": ""})
    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid Host header"}


@pytest.mark.parametrize("host", ["testserver", "localhost:3000", "cashier.example.com", "[::1]:3000"])
def test_valid_hosts_still_require_authentication(api, host):
    assert api.get("/ping", headers={"Host": host, "Authorization": ""}).status_code == 401
    assert api.get("/health", headers={"Host": host, "Authorization": ""}).status_code == 200


def test_non_ascii_bearer_is_rejected_not_crash(api):
    response = api.get("/ping", headers={b"Authorization": b"Bearer \xff"})
    assert response.status_code == 401


@pytest.mark.parametrize("query", [
    "CREATE TABLE accounts (account str)",
    "INSERT INTO accounts (account) VALUES ('Assets:Fake')",
    "CREATE TABLE external USING 'csv:/outside/secret.txt'",
    "BALANCES",
    "JOURNAL",
    "PRINT",
    "SELECT account",
    "SELECT * FROM #entries",
    "SELECT * FROM (SELECT * FROM #postings)",
    "SELECT payee FROM transactions WHERE payee IN (SELECT payee)",
    "SELECT payee FROM transactions CLOSE",
])
def test_http_bql_only_reads_original_tables(api, query):
    response = api.get("/", params={"query": query})
    assert response.status_code == 400
    accounts = api.get("/", params={"query": "SELECT account FROM #accounts ORDER BY account"})
    assert accounts.status_code == 200
    assert accounts.json() == [["Assets:Cash"], ["Expenses:Food"]]


def test_http_bql_cannot_read_synthetic_file_outside_book(api, tmp_path):
    outside = tmp_path.parent / (tmp_path.name + "-outside.txt")
    outside.write_text("SYNTHETIC_SECRET", encoding="utf-8")
    try:
        response = api.get("/", params={"query": f"CREATE TABLE external (line str) USING 'csv:{outside}'"})
        assert response.status_code == 400
        assert api.get("/", params={"query": "SELECT * FROM external"}).status_code == 400
    finally:
        outside.unlink()


@pytest.mark.parametrize("name", [".env", "unrelated.txt", ".hidden/private.bean", "backup/private.txt"])
def test_infrastructure_hides_nonledger_and_hidden_files(api, name):
    assert main.BEAN_FILE is not None
    path = Path(main.BEAN_FILE).parent / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("SYNTHETIC_SECRET", encoding="utf-8")
    response = api.get("/infrastructure", params={"file_path": name})
    assert response.status_code == 403
    assert "SYNTHETIC_SECRET" not in response.text
    glob = api.get("/infrastructure", params={"file_path": "**/*"})
    assert glob.status_code == 200
    assert "SYNTHETIC_SECRET" not in glob.text


@pytest.mark.parametrize("length", [100, 101])
def test_xact_batch_bound(api, length, monkeypatch):
    import writeback

    monkeypatch.setenv("BEANCOUNT_MANUAL_TRANSACTIONS_FILE", "configured")
    monkeypatch.setattr(writeback, "validate_and_commit", lambda texts: ({"synchronized": [], "rejected": []}, False))
    response = api.post("/xact", json={"transactions": ["invalid"] * length})
    assert response.status_code == (200 if length == 100 else 422)


def test_body_limit_includes_chunked_requests(api, monkeypatch):
    monkeypatch.setattr(http_limits, "MAX_REQUEST_BYTES", 64)
    assert api.post("/xact", content=b"a" * 65).status_code == 413
    assert api.post("/xact", content=iter([b"a" * 32, b"b" * 33])).status_code == 413
    assert api.post("/xact", headers={"Content-Length": "-1"}, content=b"x").status_code == 400
    assert api.post("/xact", headers={"Content-Length": "9" * 5000}, content=b"x").status_code == 400


def test_oversized_query_string_rejected_before_bql(api, monkeypatch):
    monkeypatch.setattr(http_limits, "MAX_QUERY_STRING_BYTES", 16)
    assert api.get("/", params={"query": "x" * 17}).status_code == 413


@pytest.mark.asyncio
async def test_body_deadline():
    async def unexpected(*args):
        pytest.fail("Expired request must not reach the app")

    async def receive():
        await anyio.sleep_forever()
        return {"type": "http.disconnect"}

    messages = []

    async def send(message):
        messages.append(message)

    from unittest.mock import patch
    with patch.object(http_limits, "BODY_TIMEOUT_SECONDS", 0.01):
        await http_limits.RequestLimitsMiddleware(unexpected)(
            {"type": "http", "headers": [], "query_string": b""}, receive, send,
        )
    assert messages[0]["status"] == 408


@pytest.mark.asyncio
async def test_blocking_writeback_runs_off_event_loop(api, monkeypatch):
    import writeback

    entered, release = threading.Event(), threading.Event()
    loop_thread = threading.get_ident()

    def blocked(texts):
        assert threading.get_ident() != loop_thread
        entered.set()
        assert release.wait(3)
        return {"synchronized": [], "rejected": []}, False

    monkeypatch.setenv("BEANCOUNT_MANUAL_TRANSACTIONS_FILE", "configured")
    monkeypatch.setattr(writeback, "validate_and_commit", blocked)
    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://testserver", headers=api.headers) as client:
        pending = asyncio.create_task(client.post("/xact", json={"transactions": []}))
        try:
            assert await anyio.to_thread.run_sync(entered.wait, 2)
            response = await asyncio.wait_for(client.get("/health"), 1)
            assert response.status_code == 200
            assert not pending.done()
        finally:
            release.set()
        assert (await pending).status_code == 200


def test_heavy_admission_limit_does_not_block_health(api, monkeypatch):
    gate = threading.BoundedSemaphore(1)
    gate.acquire()
    monkeypatch.setattr(main, "_heavy_operations", gate)
    try:
        assert api.get("/", params={"query": "SELECT 1"}).status_code == 503
        assert api.get("/infrastructure", params={"file_path": "main.bean"}).status_code == 503
        assert api.get("/health").status_code == 200
    finally:
        gate.release()


def test_committed_write_keeps_admission_slot_until_cache_refresh(api, monkeypatch):
    import writeback

    gate = threading.BoundedSemaphore(1)
    refreshed = []
    monkeypatch.setattr(main, "_heavy_operations", gate)
    monkeypatch.setenv("BEANCOUNT_MANUAL_TRANSACTIONS_FILE", "configured")
    monkeypatch.setattr(writeback, "validate_and_commit", lambda texts: ({"synchronized": ["durable"], "rejected": []}, True))

    def refresh():
        acquired = gate.acquire(blocking=False)
        if acquired:
            gate.release()
        assert not acquired, "Commit and refresh must own the same admission slot"
        refreshed.append(True)

    monkeypatch.setattr(main, "refresh_beancount_connection", refresh)
    response = api.post("/xact", json={"transactions": []})
    assert response.status_code == 200
    assert response.json() == {"synchronized": ["durable"], "rejected": []}
    assert refreshed == [True]
    assert gate.acquire(blocking=False)
    gate.release()


def test_http_json_response_limit_rejects_repeated_large_values(api):
    connection = main.app.state.connection
    entries = connection.tables["entries"].entries
    transaction = entries[-1]._replace(narration="x" * 1024)
    connection.tables["entries"].entries = entries[:2] + [transaction] * 9000
    response = api.get("/", params={"query": "SELECT narration FROM #transactions"})
    assert response.status_code == 400
    assert "HTTP JSON response exceeds byte limit" in response.json()["detail"]


def test_client_limits_do_not_leak_tokens_in_error(api):
    response = api.get("/", params={"query": "SELECT " + "x" * (16 * 1024)})
    assert response.status_code in {400, 413}
    assert "test-api-token" not in json.dumps(response.json())
