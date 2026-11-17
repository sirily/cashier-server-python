"""HTTP regression coverage for authentication, snapshot and resource boundaries."""

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
    assert glob.status_code == 403
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


def test_oversized_query_string_rejected_before_snapshot(api, monkeypatch):
    monkeypatch.setattr(http_limits, "MAX_QUERY_STRING_BYTES", 16)
    assert api.get("/infrastructure", params={"file_path": "x" * 17}).status_code == 413


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
    monkeypatch.setenv("BEANCOUNT_MANUAL_TRANSACTIONS_FILE", "configured")
    try:
        assert api.post("/xact", json={"transactions": []}).status_code == 503
        assert api.get("/infrastructure", params={"file_path": "main.bean"}).status_code == 503
        assert api.get("/health").status_code == 200
    finally:
        gate.release()


@pytest.mark.parametrize("wrote", [False, True])
def test_writeback_holds_admission_slot_during_validation_and_commit(api, monkeypatch, wrote):
    import writeback

    gate = threading.BoundedSemaphore(1)
    committed = []
    monkeypatch.setattr(main, "_heavy_operations", gate)
    monkeypatch.setenv("BEANCOUNT_MANUAL_TRANSACTIONS_FILE", "configured")

    def validate_and_commit(texts):
        acquired = gate.acquire(blocking=False)
        if acquired:
            gate.release()
        assert not acquired, "Validation and commit must own the admission slot"
        assert texts == ["transaction"]
        committed.append(True)
        return {"synchronized": ["durable"], "rejected": []}, wrote

    monkeypatch.setattr(writeback, "validate_and_commit", validate_and_commit)
    response = api.post("/xact", json={"transactions": ["transaction"]})
    assert response.status_code == 200
    assert response.json() == {"synchronized": ["durable"], "rejected": []}
    assert committed == [True]
    assert gate.acquire(blocking=False)
    gate.release()


def test_client_limits_do_not_leak_tokens_in_error(api, monkeypatch):
    monkeypatch.setattr(http_limits, "MAX_QUERY_STRING_BYTES", 64)
    response = api.get("/infrastructure", params={"file_path": "x" * 65})
    assert response.status_code == 413
    assert "test-api-token" not in json.dumps(response.json())
