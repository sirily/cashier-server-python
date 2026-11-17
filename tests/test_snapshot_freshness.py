"""Each snapshot and writeback must use the current authoritative source tree."""


import pytest
from beancount import loader
from beancount.core import data
from fastapi.testclient import TestClient

import main

ACCOUNTS = (
    "2024-01-01 open Assets:Cash USD\n"
    "2024-01-01 open Expenses:Food USD\n"
    "2024-01-01 open Expenses:Temporary USD\n"
)


def transaction(cashier_id, account="Expenses:Food", narration="Fresh purchase"):
    return (
        f'2024-01-06 * "Shop" "{narration}"\n'
        f'  cashier_id: "{cashier_id}"\n'
        "  Assets:Cash -1.00 USD\n"
        f"  {account} 1.00 USD\n"
    )


@pytest.fixture
def freshness_api(monkeypatch, tmp_path):
    directory = tmp_path / "ledger"
    directory.mkdir()
    (directory / "main.bean").write_text(
        'include "accounts.bean"\n'
        'include "transactions/*.bean"\n'
        'include "manual_transactions.bean"\n',
        encoding="utf-8",
    )
    (directory / "accounts.bean").write_text(ACCOUNTS, encoding="utf-8")
    (directory / "transactions").mkdir()
    (directory / "transactions/first.bean").write_text(
        transaction("external-first", narration="Before edit"), encoding="utf-8",
    )
    (directory / "manual_transactions.bean").write_text("", encoding="utf-8")
    monkeypatch.setenv("BEANCOUNT_FILE", str(directory / "main.bean"))
    monkeypatch.setenv(
        "BEANCOUNT_MANUAL_TRANSACTIONS_FILE", str(directory / "manual_transactions.bean"),
    )
    monkeypatch.setattr(main, "BEAN_FILE", str(directory / "main.bean"))
    monkeypatch.setattr(main, "CASHIER_API_TOKEN", "freshness-test-token")
    with TestClient(main.app, headers={"Authorization": "Bearer freshness-test-token"}) as client:
        yield client, directory


def snapshot(client):
    response = client.get("/infrastructure", params={"file_path": "main.bean"})
    assert response.status_code == 200, response.text
    content = response.json()["content"]
    entries, errors, _ = loader.load_string(content)
    assert not errors
    assert 'include "' not in content
    return content, entries


def test_external_included_file_edit_is_visible_on_next_snapshot(freshness_api):
    client, directory = freshness_api
    before, entries = snapshot(client)
    assert "Before edit" in before
    assert [entry.narration for entry in entries if isinstance(entry, data.Transaction)] == ["Before edit"]

    source = transaction("external-first", narration="After edit")
    (directory / "transactions/first.bean").write_text(source, encoding="utf-8")

    after, entries = snapshot(client)
    assert source in after
    assert "Before edit" not in after
    assert after != before
    assert [entry.narration for entry in entries if isinstance(entry, data.Transaction)] == ["After edit"]


def test_new_glob_match_is_visible_on_next_snapshot(freshness_api):
    client, directory = freshness_api
    before, _ = snapshot(client)
    assert "external-second" not in before

    source = transaction("external-second", narration="New glob source")
    (directory / "transactions/second.bean").write_text(source, encoding="utf-8")

    after, entries = snapshot(client)
    assert source in after
    assert after != before
    ids = [entry.meta.get("cashier_id") for entry in entries if isinstance(entry, data.Transaction)]
    assert len(ids) == 2
    assert set(ids) == {"external-first", "external-second"}


@pytest.mark.parametrize("failure", ["missing_include", "deleted_include", "symlink", "parser", "materialization"])
def test_snapshot_failure_never_serves_previous_content_and_recovers(freshness_api, failure):
    client, directory = freshness_api
    before, _ = snapshot(client)
    root = directory / "main.bean"
    accounts = directory / "accounts.bean"
    original_root = root.read_text(encoding="utf-8")
    original_accounts = accounts.read_text(encoding="utf-8")

    if failure == "missing_include":
        root.write_text(original_root + 'include "missing.bean"\n', encoding="utf-8")
    elif failure == "deleted_include":
        accounts.unlink()
    elif failure == "symlink":
        outside = directory.parent / "outside.bean"
        outside.write_text(original_accounts, encoding="utf-8")
        accounts.unlink()
        accounts.symlink_to(outside)
    elif failure == "parser":
        accounts.write_text("this is not a Beancount directive\n", encoding="utf-8")
    else:
        accounts.write_text(
            original_accounts + "2024-01-10 balance Assets:Cash 999.00 USD\n", encoding="utf-8",
        )

    response = client.get("/infrastructure", params={"file_path": "main.bean"})
    assert response.status_code == 422
    error = response.json()
    assert "content" not in error
    assert error["detail"]["message"]
    assert error["detail"]["errors"]
    assert before not in response.text

    root.write_text(original_root, encoding="utf-8")
    if accounts.is_symlink():
        accounts.unlink()
    accounts.write_text(original_accounts, encoding="utf-8")
    updated = transaction("external-first", narration="Recovered current source")
    (directory / "transactions/first.bean").write_text(updated, encoding="utf-8")

    recovered, _ = snapshot(client)
    assert updated in recovered
    assert "Before edit" not in recovered


def test_writeback_accepts_account_added_externally_after_first_snapshot(freshness_api):
    client, directory = freshness_api
    before, _ = snapshot(client)
    assert "Expenses:Added" not in before
    manual = directory / "manual_transactions.bean"
    original = manual.read_bytes()
    text = transaction("new-account", account="Expenses:Added")

    unknown = client.post("/xact", json={"transactions": [text]})
    assert unknown.status_code == 200
    assert unknown.json()["synchronized"] == []
    assert unknown.json()["rejected"][0]["cashier_id"] == "new-account"
    assert "Unknown account" in unknown.json()["rejected"][0]["reason"]
    assert manual.read_bytes() == original

    (directory / "accounts.bean").write_text(
        ACCOUNTS + "2024-01-01 open Expenses:Added USD\n", encoding="utf-8",
    )
    accepted = client.post("/xact", json={"transactions": [text]})
    assert accepted.status_code == 200
    assert accepted.json() == {"synchronized": ["new-account"], "rejected": []}
    stored = manual.read_text(encoding="utf-8")
    assert stored.count('cashier_id: "new-account"') == 1
    after, entries = snapshot(client)
    assert after.count('cashier_id: "new-account"') == 1
    assert any(isinstance(entry, data.Open) and entry.account == "Expenses:Added" for entry in entries)
    matching = [
        entry for entry in entries
        if isinstance(entry, data.Transaction) and entry.meta.get("cashier_id") == "new-account"
    ]
    assert len(matching) == 1
    assert [posting.account for posting in matching[0].postings] == ["Assets:Cash", "Expenses:Added"]


def test_writeback_rejects_account_removed_externally_before_next_request(freshness_api):
    client, directory = freshness_api
    before, _ = snapshot(client)
    assert "Expenses:Temporary" in before
    accepted = client.post("/xact", json={"transactions": [transaction("first-valid")]})
    assert accepted.status_code == 200
    assert accepted.json() == {"synchronized": ["first-valid"], "rejected": []}
    manual = directory / "manual_transactions.bean"
    committed = manual.read_bytes()

    (directory / "accounts.bean").write_text(
        ACCOUNTS.replace("2024-01-01 open Expenses:Temporary USD\n", ""), encoding="utf-8",
    )
    rejected = client.post("/xact", json={
        "transactions": [transaction("removed-account", account="Expenses:Temporary")],
    })
    assert rejected.status_code == 200
    result = rejected.json()
    assert result["synchronized"] == []
    assert len(result["rejected"]) == 1
    assert result["rejected"][0]["cashier_id"] == "removed-account"
    assert "Unknown account" in result["rejected"][0]["reason"]
    assert "Expenses:Temporary" in result["rejected"][0]["reason"]
    assert manual.read_bytes() == committed
    after, _ = snapshot(client)
    assert "Expenses:Temporary" not in after
    assert 'cashier_id: "first-valid"' in after
    assert "removed-account" not in after


def test_invalid_current_full_ledger_blocks_append_after_earlier_valid_write(freshness_api):
    client, directory = freshness_api
    snapshot(client)
    accepted = client.post("/xact", json={"transactions": [transaction("earlier-valid")]})
    assert accepted.status_code == 200
    assert accepted.json() == {"synchronized": ["earlier-valid"], "rejected": []}
    snapshot(client)
    manual = directory / "manual_transactions.bean"
    committed = manual.read_bytes()

    (directory / "accounts.bean").write_text(
        ACCOUNTS + "2024-01-10 balance Assets:Cash 999.00 USD\n", encoding="utf-8",
    )
    text = transaction("after-ledger-error")
    rejected = client.post("/xact", json={"transactions": [text]})
    assert rejected.status_code == 500
    assert "Authoritative full ledger has errors" in rejected.json()["detail"]
    assert manual.read_bytes() == committed
    failed_pull = client.get("/infrastructure", params={"file_path": "main.bean"})
    assert failed_pull.status_code == 422
    assert "content" not in failed_pull.json()

    (directory / "accounts.bean").write_text(ACCOUNTS, encoding="utf-8")
    recovered = client.post("/xact", json={"transactions": [text]})
    assert recovered.status_code == 200
    assert recovered.json() == {"synchronized": ["after-ledger-error"], "rejected": []}
    assert manual.read_bytes().startswith(committed)
    content, _ = snapshot(client)
    assert content.count('cashier_id: "earlier-valid"') == 1
    assert content.count('cashier_id: "after-ledger-error"') == 1
