"""Tests for Stage 2 manual transaction writeback endpoint and logic."""

import os
from pathlib import Path
import re
import tomllib

import pytest
from fastapi.testclient import TestClient

import main

client = TestClient(main.app)
TEST_DIR = os.path.dirname(os.path.abspath(__file__))

# A simple valid ledger with accounts that tests can reference
LEDGER_WITH_ACCOUNTS = os.path.join(TEST_DIR, "materialized_root.bean")


@pytest.fixture
def configured_env(monkeypatch, tmp_path):
    """Set BEANCOUNT_FILE and BEANCOUNT_MANUAL_TRANSACTIONS_FILE
    with a proper ledger structure where main.bean includes manual_transactions.bean."""
    # Read the base ledger content (has account opens, no failing plugins)
    base = Path(LEDGER_WITH_ACCOUNTS).read_text()

    # Create main.bean that includes manual_transactions.bean
    main_bean = tmp_path / "main.bean"
    main_bean.write_text(base.strip() + '\ninclude "manual_transactions.bean"\n', encoding="utf-8")

    # Create empty manual_transactions.bean inside the same ledger root
    manual_file = tmp_path / "manual_transactions.bean"
    manual_file.write_text("")

    monkeypatch.setenv("BEANCOUNT_FILE", str(main_bean))
    monkeypatch.setattr(main, "BEAN_FILE", str(main_bean))
    monkeypatch.setenv("BEANCOUNT_MANUAL_TRANSACTIONS_FILE", str(manual_file))

    yield str(manual_file)


def test_normal_append(configured_env):
    """Normal expense: POST /xact appends exactly once to manual_transactions.bean."""
    text = (
        '2026-05-28 * "Coffee"\n'
        '  cashier_id: "abc-123"\n'
        "  Assets:Cash -5.00 USD\n"
        "  Equity:Opening-Balances 5.00 USD"
    )
    response = client.post("/xact", json={"transactions": [text]})
    assert response.status_code == 200
    data = response.json()
    assert data["synchronized"] == ["abc-123"]
    assert data["rejected"] == []

    # Verify it was appended to the file
    with open(configured_env) as f:
        content = f.read()
    assert "abc-123" in content
    assert "Coffee" in content
    assert "cashier_id:" in content


def test_retry_same_id_no_duplicate(configured_env):
    """Same cashier_id + same transaction returns synchronized without duplicate."""
    text = (
        '2026-05-28 * "Test"\n'
        '  cashier_id: "dup-001"\n'
        "  Assets:Cash -10.00 USD\n"
        "  Equity:Opening-Balances 10.00 USD"
    )
    # First POST
    r1 = client.post("/xact", json={"transactions": [text]})
    assert r1.status_code == 200
    assert r1.json()["synchronized"] == ["dup-001"]

    # Second POST (same)
    r2 = client.post("/xact", json={"transactions": [text]})
    assert r2.status_code == 200
    assert r2.json()["synchronized"] == ["dup-001"]

    # File should contain exactly one occurrence
    with open(configured_env) as f:
        content = f.read()
    assert content.count("dup-001") == 1


def test_same_id_different_content_conflict(configured_env):
    """Same cashier_id with different transaction content is rejected."""
    text1 = (
        '2026-05-28 * "First"\n'
        '  cashier_id: "conflict-1"\n'
        "  Assets:Cash -10.00 USD\n"
        "  Equity:Opening-Balances 10.00 USD"
    )
    text2 = (
        '2026-05-29 * "Second different"\n'
        '  cashier_id: "conflict-1"\n'
        "  Assets:Cash -20.00 USD\n"
        "  Equity:Opening-Balances 20.00 USD"
    )

    # First POST
    r1 = client.post("/xact", json={"transactions": [text1]})
    assert r1.status_code == 200
    assert r1.json()["synchronized"] == ["conflict-1"]

    # Second POST with different content
    r2 = client.post("/xact", json={"transactions": [text2]})
    assert r2.status_code == 200
    assert r2.json()["synchronized"] == []
    assert len(r2.json()["rejected"]) == 1
    assert r2.json()["rejected"][0]["cashier_id"] == "conflict-1"
    assert "conflict" in r2.json()["rejected"][0]["reason"].lower()


def test_reject_non_transaction_directive(configured_env):
    """Open directive is rejected."""
    text = '2026-05-28 open Assets:Fake USD'
    response = client.post("/xact", json={"transactions": [text]})
    assert response.status_code == 200
    data = response.json()
    assert data["synchronized"] == []
    assert len(data["rejected"]) == 1
    assert "Open" in data["rejected"][0]["reason"]


def test_reject_incomplete_transaction(configured_env):
    """Incomplete ! transaction is rejected."""
    text = (
        '2026-05-28 ! "Incomplete"\n'
        '  cashier_id: "inc-001"\n'
        "  Assets:Cash -10.00 USD\n"
        "  Equity:Opening-Balances 10.00 USD"
    )
    response = client.post("/xact", json={"transactions": [text]})
    assert response.status_code == 200
    data = response.json()
    assert data["synchronized"] == []
    assert len(data["rejected"]) == 1
    assert "!" in data["rejected"][0]["reason"]


def test_reject_unknown_account(configured_env):
    """Transaction referencing an account not in the ledger is rejected."""
    text = (
        '2026-05-28 * "Unknown"\n'
        '  cashier_id: "unk-001"\n'
        "  Expenses:Fake 10.00 USD\n"
        "  Assets:Cash -10.00 USD"
    )
    response = client.post("/xact", json={"transactions": [text]})
    assert response.status_code == 200
    data = response.json()
    assert data["synchronized"] == []
    assert len(data["rejected"]) == 1
    assert "Unknown account" in data["rejected"][0]["reason"]


def test_mixed_batch_partial_success(configured_env):
    """Valid and invalid transactions: valid commit, invalid rejected."""
    good = (
        '2026-05-28 * "Good"\n'
        '  cashier_id: "batch-good"\n'
        "  Assets:Cash -5.00 USD\n"
        "  Equity:Opening-Balances 5.00 USD"
    )
    bad = (
        '2026-05-28 * "Bad unknown"\n'
        '  cashier_id: "batch-bad"\n'
        "  Expenses:Unknown 10.00 USD\n"
        "  Assets:Cash -10.00 USD"
    )
    response = client.post("/xact", json={"transactions": [good, bad]})
    assert response.status_code == 200
    data = response.json()
    assert "batch-good" in data["synchronized"]
    assert len(data["rejected"]) == 1
    assert data["rejected"][0]["cashier_id"] == "batch-bad"

    # Only the valid transaction was written
    with open(configured_env) as f:
        content = f.read()
    assert "batch-good" in content
    assert "batch-bad" not in content


def test_mixed_batch_unbalanced_partial_success(configured_env):
    """Valid transaction commits while unbalanced transaction is rejected."""
    good = (
        '2026-05-28 * "Good"\n'
        '  cashier_id: "batch-good"\n'
        "  Assets:Cash -5.00 USD\n"
        "  Equity:Opening-Balances 5.00 USD"
    )
    bad = (
        '2026-05-28 * "Bad unbalanced"\n'
        '  cashier_id: "batch-unbalanced"\n'
        "  Assets:Cash -5.00 USD\n"
        "  Equity:Opening-Balances 4.00 USD"
    )
    response = client.post("/xact", json={"transactions": [good, bad]})
    assert response.status_code == 200
    data = response.json()
    assert data["synchronized"] == ["batch-good"]
    assert len(data["rejected"]) == 1
    assert data["rejected"][0]["cashier_id"] == "batch-unbalanced"
    assert "validation failed" in data["rejected"][0]["reason"].lower()

    with open(configured_env) as f:
        content = f.read()
    assert "batch-good" in content
    assert "batch-unbalanced" not in content


def test_unbalanced_only_rejected_without_synchronized(configured_env):
    """An unbalanced transaction must not be reported synchronized."""
    bad = (
        '2026-05-28 * "Bad unbalanced"\n'
        '  cashier_id: "only-unbalanced"\n'
        "  Assets:Cash -5.00 USD\n"
        "  Equity:Opening-Balances 4.00 USD"
    )
    response = client.post("/xact", json={"transactions": [bad]})
    assert response.status_code == 200
    data = response.json()
    assert data["synchronized"] == []
    assert len(data["rejected"]) == 1
    assert data["rejected"][0]["cashier_id"] == "only-unbalanced"

    with open(configured_env) as f:
        content = f.read()
    assert content == ""


def test_missing_cashier_id_rejected(configured_env):
    """Transaction without cashier_id is rejected."""
    text = (
        '2026-05-28 * "No id"\n'
        "  Assets:Cash -5.00 USD\n"
        "  Equity:Opening-Balances 5.00 USD"
    )
    response = client.post("/xact", json={"transactions": [text]})
    assert response.status_code == 200
    data = response.json()
    assert data["synchronized"] == []
    assert len(data["rejected"]) == 1
    assert "cashier_id" in data["rejected"][0]["reason"].lower()


def test_env_not_configured_returns_500(monkeypatch):
    """When BEANCOUNT_MANUAL_TRANSACTIONS_FILE is unset, endpoint returns 500."""
    monkeypatch.delenv("BEANCOUNT_MANUAL_TRANSACTIONS_FILE", raising=False)
    text = (
        '2026-05-28 * "Test"\n'
        '  cashier_id: "abc-123"\n'
        "  Assets:Cash -5.00 USD\n"
        "  Equity:Opening-Balances 5.00 USD"
    )
    response = client.post("/xact", json={"transactions": [text]})
    assert response.status_code == 500


def test_no_client_controlled_path(configured_env):
    """Request body must not contain a file path."""
    malicious_path = Path(configured_env).with_name("malicious.bean")
    text = (
        '2026-05-28 * "Test"\n'
        '  cashier_id: "abc-123"\n'
        "  Assets:Cash -5.00 USD\n"
        "  Equity:Opening-Balances 5.00 USD"
    )
    response = client.post(
        "/xact",
        json={"transactions": [text], "file_path": str(malicious_path)},
    )
    # Our Pydantic model ignores extra fields, so it should still work
    # but the file path in body must never be accepted.
    # The important part: no file_path parameter exists in the model.
    assert response.status_code == 200
    assert not malicious_path.exists()
    assert "abc-123" in Path(configured_env).read_text(encoding="utf-8")


def test_balance_directive_rejected(configured_env):
    """Balance directive is rejected as non-transaction."""
    text = '2026-05-28 balance Assets:Cash 0.00 USD'
    response = client.post("/xact", json={"transactions": [text]})
    assert response.status_code == 200
    data = response.json()
    assert data["synchronized"] == []
    assert "Balance" in data["rejected"][0]["reason"]


def test_pad_directive_rejected(configured_env):
    """Pad directive is rejected."""
    text = '2026-05-28 pad Assets:Cash Equity:Opening-Balances'
    response = client.post("/xact", json={"transactions": [text]})
    assert response.status_code == 200
    data = response.json()
    assert data["synchronized"] == []
    assert "Pad" in data["rejected"][0]["reason"]


def test_include_directive_rejected(configured_env):
    """Include directive is rejected."""
    text = 'include "test.bean"'
    response = client.post("/xact", json={"transactions": [text]})
    assert response.status_code == 200
    data = response.json()
    assert data["synchronized"] == []
    assert len(data["rejected"]) == 1


def test_no_temp_files_in_ledger_workspace(configured_env, monkeypatch):
    """Assert commit does not create temp files in the ledger workspace.

    Monkeypatches tempfile.mkstemp to fail: the corrected implementation must
    not call mkstemp(dir=ledger_workspace) for the commit path.
    """
    import tempfile as tf

    def failing_mkstemp(*args, **kwargs):
        raise RuntimeError("mkstemp should not be called during commit")

    monkeypatch.setattr(tf, "mkstemp", failing_mkstemp)

    text = (
        '2026-05-28 * "Coffee"\n'
        '  cashier_id: "abc-123"\n'
        "  Assets:Cash -5.00 USD\n"
        "  Equity:Opening-Balances 5.00 USD"
    )
    response = client.post("/xact", json={"transactions": [text]})
    assert response.status_code == 200
    assert response.json()["synchronized"] == ["abc-123"]


def test_commit_opens_manual_file_in_append_mode(configured_env, monkeypatch):
    """Writeback must not truncate the single mounted manual file before writing."""
    import builtins

    original_open = builtins.open
    modes = []

    def tracking_open(file, mode="r", *args, **kwargs):
        if os.fspath(file) == configured_env:
            modes.append(mode)
        return original_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", tracking_open)

    text = (
        '2026-05-28 * "Coffee"\n'
        '  cashier_id: "abc-123"\n'
        "  Assets:Cash -5.00 USD\n"
        "  Equity:Opening-Balances 5.00 USD"
    )
    response = client.post("/xact", json={"transactions": [text]})
    assert response.status_code == 200
    assert response.json()["synchronized"] == ["abc-123"]
    assert "a" in modes
    assert "w" not in modes


def test_full_ledger_validation_uses_temp_copy(configured_env, monkeypatch):
    """Prove /xact invokes full-ledger validation on a copied root,
    not only parser.parse_string(candidate)."""
    import writeback

    original_load_file = writeback.loader.load_file
    validation_paths = []

    def tracking_load_file(path, *args, **kwargs):
        validation_paths.append(str(path))
        return original_load_file(path, *args, **kwargs)

    monkeypatch.setattr(writeback.loader, "load_file", tracking_load_file)

    text = (
        '2026-05-28 * "Coffee"\n'
        '  cashier_id: "abc-123"\n'
        "  Assets:Cash -5.00 USD\n"
        "  Equity:Opening-Balances 5.00 USD"
    )
    response = client.post("/xact", json={"transactions": [text]})
    assert response.status_code == 200
    assert response.json()["synchronized"] == ["abc-123"]

    # load_file should have been called at least twice:
    # 1. _load_full_ledger() on the original BEANCOUNT_FILE
    # 2. _validate_candidate_as_full_ledger() on a temp copy
    assert len(validation_paths) >= 2

    # At least one call must be to a path different from the original
    bean_file = os.environ["BEANCOUNT_FILE"]
    temp_copy_calls = [p for p in validation_paths if p != bean_file]
    assert len(temp_copy_calls) >= 1, (
        "Expected load_file to be called on a temp copy (not the original BEANCOUNT_FILE)"
    )


def test_existing_manual_parse_error_fails_closed(configured_env):
    """Malformed existing manual file must not be overwritten."""
    manual = Path(configured_env)
    original = (
        '2026-05-01 * "Broken"\n'
        '  cashier_id: "broken-existing"\n'
        "  this is not valid beancount syntax\n"
    )
    manual.write_text(original, encoding="utf-8")

    text = (
        '2026-05-28 * "Coffee"\n'
        '  cashier_id: "abc-123"\n'
        "  Assets:Cash -5.00 USD\n"
        "  Equity:Opening-Balances 5.00 USD"
    )
    response = client.post("/xact", json={"transactions": [text]})
    assert response.status_code == 500
    assert "parse errors" in response.json()["detail"]
    assert manual.read_text(encoding="utf-8") == original


def test_existing_manual_duplicate_cashier_id_fails_closed(configured_env):
    """Ambiguous existing idempotency state must not accept new writes."""
    manual = Path(configured_env)
    original = (
        '2026-05-01 * "Old 1"\n'
        '  cashier_id: "dup-existing"\n'
        "  Assets:Cash -1.00 USD\n"
        "  Equity:Opening-Balances 1.00 USD\n"
        "\n"
        '2026-05-02 * "Old 2"\n'
        '  cashier_id: "dup-existing"\n'
        "  Assets:Cash -2.00 USD\n"
        "  Equity:Opening-Balances 2.00 USD\n"
    )
    manual.write_text(original, encoding="utf-8")

    text = (
        '2026-05-28 * "Coffee"\n'
        '  cashier_id: "abc-123"\n'
        "  Assets:Cash -5.00 USD\n"
        "  Equity:Opening-Balances 5.00 USD"
    )
    response = client.post("/xact", json={"transactions": [text]})
    assert response.status_code == 500
    assert "duplicate cashier_id dup-existing" in response.json()["detail"]
    assert manual.read_text(encoding="utf-8") == original


def test_write_permission_error_returns_json_500(configured_env, monkeypatch):
    """Filesystem write failures should be controlled JSON errors, not tracebacks."""
    import writeback

    def failing_write(filepath, previous_content, candidate_content):
        raise PermissionError(13, "Permission denied", filepath)

    monkeypatch.setattr(writeback, "_write_file_crash_conscious", failing_write)

    text = (
        '2026-05-28 * "Coffee"\n'
        '  cashier_id: "abc-123"\n'
        "  Assets:Cash -5.00 USD\n"
        "  Equity:Opening-Balances 5.00 USD"
    )
    response = client.post("/xact", json={"transactions": [text]})
    assert response.status_code == 500
    assert "Permission denied" in response.json()["detail"]
    assert Path(configured_env).read_text(encoding="utf-8") == ""


def test_refresh_failure_after_write_still_returns_synchronized(configured_env, monkeypatch):
    """A post-commit cache refresh failure must not make the client retry a committed write."""

    def failing_refresh():
        raise RuntimeError("reload failed")

    monkeypatch.setattr(main, "refresh_beancount_connection", failing_refresh)

    text = (
        '2026-05-28 * "Coffee"\n'
        '  cashier_id: "abc-123"\n'
        "  Assets:Cash -5.00 USD\n"
        "  Equity:Opening-Balances 5.00 USD"
    )
    response = client.post("/xact", json={"transactions": [text]})
    assert response.status_code == 200
    assert response.json() == {"synchronized": ["abc-123"], "rejected": []}
    assert "abc-123" in Path(configured_env).read_text(encoding="utf-8")


def test_append_preserves_existing_manual_source_comments(configured_env):
    """Appending must not reprint or drop existing manual source text."""
    manual = Path(configured_env)
    existing = (
        "; user comment that must survive\n"
        "\n"
        '2026-05-01 * "Old"\n'
        '  cashier_id: "old-1"\n'
        "  Assets:Cash -1.00 USD\n"
        "  Equity:Opening-Balances 1.00 USD\n"
    )
    manual.write_text(existing, encoding="utf-8")

    text = (
        '2026-05-28 * "Coffee"\n'
        '  cashier_id: "abc-123"\n'
        "  Assets:Cash -5.00 USD\n"
        "  Equity:Opening-Balances 5.00 USD"
    )
    response = client.post("/xact", json={"transactions": [text]})
    assert response.status_code == 200
    content = manual.read_text(encoding="utf-8")
    assert content.startswith(existing)
    assert "; user comment that must survive" in content
    assert "abc-123" in content


def test_packaging_includes_writeback_module():
    """Wheel packaging must include runtime writeback.py module."""
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    only_include = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["only-include"]
    assert "writeback.py" in only_include


def test_docker_image_includes_writeback_module():
    """Docker image must copy runtime writeback.py module."""
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"^COPY .*writeback\.py", dockerfile, re.MULTILINE)
