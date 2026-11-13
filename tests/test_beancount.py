"""BQL and writeback/pull regressions for the Beancount-only server."""

from pathlib import Path

import pytest
from beancount.core import data
from beancount.loader import load_string
from fastapi.testclient import TestClient

import main


@pytest.fixture
def beancount_client(monkeypatch, tmp_path):
    book = tmp_path / "main.bean"
    book.write_text(
        '2024-01-01 open Assets:Cash USD\n'
        '2024-01-01 open Expenses:Food USD\n'
        '2024-01-02 * "Cafe" "Coffee"\n'
        '  Assets:Cash -5.00 USD\n'
        '  Expenses:Food 5.00 USD\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(main, "BEAN_FILE", str(book))
    monkeypatch.setattr(main, "CASHIER_API_TOKEN", "test-api-token")
    monkeypatch.setattr(main.app.state, "connection", main.preload_beancount_data(), raising=False)
    return TestClient(main.app, headers={"Authorization": "Bearer test-api-token"})


@pytest.mark.asyncio
async def test_beancount_method(beancount_client):
    assert await main.beancount("SELECT 1 LIMIT 1") == [(1,)]


@pytest.mark.parametrize("query,expected", [
    ("SELECT account FROM #accounts WHERE close IS NULL ORDER BY account",
     [["Assets:Cash"], ["Expenses:Food"]]),
    ("SELECT DISTINCT COALESCE(payee, narration) AS payee FROM transactions ORDER BY payee",
     [["Cafe"]]),
])
def test_accounts_and_payees_queries(beancount_client, query, expected):
    response = beancount_client.get("/", params={"query": query})

    assert response.status_code == 200
    assert response.json() == expected


def test_reload_refreshes_bql_connection(beancount_client):
    assert main.BEAN_FILE is not None
    with Path(main.BEAN_FILE).open("a", encoding="utf-8") as book:
        book.write('2024-01-01 open Expenses:Travel USD\n')

    response = beancount_client.get("/reload")
    accounts = beancount_client.get("/", params={"query": "SELECT account FROM #accounts"})

    assert response.status_code == 200
    assert accounts.status_code == 200
    assert ["Expenses:Travel"] in accounts.json()


def test_writeback_pull_and_cashier_id_confirmation(configured_env, monkeypatch):
    monkeypatch.setattr(main, "CASHIER_API_TOKEN", "test-api-token")
    monkeypatch.setattr(main.app.state, "connection", main.preload_beancount_data(), raising=False)
    client = TestClient(main.app, headers={"Authorization": "Bearer test-api-token"})
    transaction = (
        '2026-05-28 * "Cafe" "Coffee"\n'
        '  cashier_id: "sync-regression-17"\n'
        '  Assets:Cash -5.00 USD\n'
        '  Equity:Opening-Balances 5.00 USD\n'
    )

    pushed = client.post("/xact", json={"transactions": [transaction]})
    assert pushed.status_code == 200
    assert pushed.json() == {"synchronized": ["sync-regression-17"], "rejected": []}

    payees = client.get("/", params={"query": "SELECT payee FROM transactions WHERE payee IS NOT NULL"})
    assert payees.status_code == 200
    assert payees.json() == [["Cafe"]]

    pulled = client.get("/infrastructure", params={"file_path": "main.bean"})
    assert pulled.status_code == 200
    entries, errors, _ = load_string(pulled.json()["content"])
    assert errors == []
    matching = [
        entry for entry in entries
        if isinstance(entry, data.Transaction) and entry.meta.get("cashier_id") == "sync-regression-17"
    ]
    assert len(matching) == 1
    assert matching[0].payee == "Cafe"

    retried = client.post("/xact", json={"transactions": [transaction]})
    assert retried.status_code == 200
    assert retried.json() == pushed.json()
    assert Path(configured_env).read_text(encoding="utf-8").count("sync-regression-17") == 1
