"""Writeback and snapshot confirmation regressions for the Beancount server."""

from pathlib import Path

from beancount.core import data
from beancount.loader import load_string
from fastapi.testclient import TestClient

import main


def test_writeback_pull_and_cashier_id_confirmation(configured_env, monkeypatch):
    monkeypatch.setattr(main, "CASHIER_API_TOKEN", "test-api-token")
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
