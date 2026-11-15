"""Writeback must not use a ledger's display precision as storage precision."""
from decimal import Decimal
from pathlib import Path

import pytest
from beancount import loader
from beancount.core import data
from beancount.parser import parser

import writeback
from tests.test_writeback import client


def transaction(number="7.25"):
    return (
        '2026-05-28 * "Decimal"\n'
        '  cashier_id: "decimal-regression"\n'
        f'  Assets:Cash -{number} USD\n'
        f'  Equity:Opening-Balances {number} USD\n'
    )


@pytest.mark.parametrize("same_batch", [False, True])
def test_post_preserves_decimal_amounts(configured_env, same_batch):
    _, errors, options = loader.load_file(str(Path(configured_env).with_name("main.bean")))
    assert not errors
    assert options["dcontext"].quantize(Decimal("7.25"), "USD") == Decimal(7)

    transactions = [transaction(), transaction()] if same_batch else [transaction()]
    response = client.post("/xact", json={"transactions": transactions})
    assert response.json() == {"synchronized": ["decimal-regression"], "rejected": []}
    entries, errors, _ = parser.parse_string(Path(configured_env).read_text())
    assert not errors
    entry = entries[0]
    assert isinstance(entry, data.Transaction)
    numbers = []
    for posting in entry.postings:
        assert posting.units is not None
        numbers.append(posting.units.number)
    assert numbers == [Decimal("-7.25"), Decimal("7.25")]
    assert len(entries) == 1
    before = Path(configured_env).read_bytes()
    assert client.post("/xact", json={"transactions": [transaction()]}).json()["rejected"] == []
    assert Path(configured_env).read_bytes() == before


@pytest.mark.parametrize("same_batch", [False, True])
def test_decimal_difference_is_conflict(configured_env, same_batch):
    first, second = transaction("7.21"), transaction("7.25")
    if not same_batch:
        assert client.post("/xact", json={"transactions": [first]}).json()["rejected"] == []
    response = client.post("/xact", json={"transactions": [first, second] if same_batch else [second]})
    assert len(response.json()["rejected"]) == 1
    assert "conflict" in response.json()["rejected"][0]["reason"].lower()


@pytest.mark.parametrize("annotation", ["", " {2.123456789 USD}", " {2.123456789 # 0.000000123 USD}", " @ 2.123456789 USD"])
def test_append_and_canonicalization_preserve_all_precision(annotation):
    text = (
        '2026-05-28 * "Precision"\n'
        '  cashier_id: "precision"\n'
        f'  Assets:Cash 7.123456789 USD{annotation}\n'
        '  Equity:Opening-Balances -7.123456789 USD\n'
    )
    entries, errors, _ = parser.parse_string(text)
    assert not errors
    entry = entries[0]
    assert isinstance(entry, data.Transaction)
    for rendered in (writeback._normalize_entry(entry), writeback._append_entries_to_source("; untouched\n", entries)):
        reparsed, errors, _ = parser.parse_string(rendered)
        assert not errors
        reparsed_entry = reparsed[0]
        assert isinstance(reparsed_entry, data.Transaction)
        assert [p._replace(meta=None) for p in reparsed_entry.postings] == [p._replace(meta=None) for p in entry.postings]
    changed, errors, _ = parser.parse_string(text.replace("123456789", "123456788"))
    assert not errors
    changed_entry = changed[0]
    assert isinstance(changed_entry, data.Transaction)
    assert writeback._normalize_entry(entry) != writeback._normalize_entry(changed_entry)


def test_crlf_unicode_prefix_and_high_precision_survive_retries(configured_env):
    manual = Path(configured_env)
    prefix = "; café original source\r\n; untouched comment\r\n".encode()
    manual.write_bytes(prefix)
    text = transaction("7.123456789123456789")
    first, wrote = writeback.validate_and_commit([text, text])
    assert wrote
    assert first == {"synchronized": ["decimal-regression"], "rejected": []}
    committed = manual.read_bytes()
    assert committed.startswith(prefix)
    writeback._check_writeback_integrity(committed)
    retry, wrote = writeback.validate_and_commit([text])
    assert not wrote
    assert retry == first
    assert manual.read_bytes() == committed
    entries, errors, _ = parser.parse_string(committed)
    assert not errors
    entry = entries[0]
    assert isinstance(entry, data.Transaction)
    assert entry.postings[0].units is not None
    assert entry.postings[0].units.number == Decimal("-7.123456789123456789")


def test_reordered_metadata_and_formatting_are_idempotent_without_rounding(configured_env):
    original = transaction("7.123456789123456789").replace(
        '  cashier_id: "decimal-regression"\n',
        '  cashier_id: "decimal-regression"\n  zeta: "last"\n  alpha: "first"\n',
    ).replace(
        '  Equity:Opening-Balances',
        '    zeta: "posting-last"\n    alpha: "posting-first"\n  Equity:Opening-Balances',
    )
    reordered = original.replace(
        '  cashier_id: "decimal-regression"\n  zeta: "last"\n  alpha: "first"',
        '  alpha: "first"\n  zeta: "last"\n  cashier_id: "decimal-regression"',
    ).replace(
        '    zeta: "posting-last"\n    alpha: "posting-first"',
        '    alpha: "posting-first"\n    zeta: "posting-last"',
    ).replace("  Assets:Cash -", "  Assets:Cash    -")
    result, wrote = writeback.validate_and_commit([original, reordered])
    assert wrote
    assert result == {"synchronized": ["decimal-regression"], "rejected": []}
    committed = Path(configured_env).read_bytes()
    result, wrote = writeback.validate_and_commit([reordered])
    assert not wrote
    assert result == {"synchronized": ["decimal-regression"], "rejected": []}
    assert Path(configured_env).read_bytes() == committed
    changed = reordered.replace("7.123456789123456789", "7.123456789123456788")
    result, wrote = writeback.validate_and_commit([changed])
    assert not wrote
    assert "Conflict" in result["rejected"][0]["reason"]
    assert Path(configured_env).read_bytes() == committed


@pytest.mark.parametrize("label, preserves_semantics", [
    ("ordinary lot", True), ("first line\nsecond line", True),
    ('ordinary "quote"', False), ("ordinary\\label", False), ("", False),
])
def test_lot_label_serialization_requires_exact_semantic_roundtrip(label, preserves_semantics):
    quoted = '"' + label.replace("\\", "\\\\").replace('"', '\\"') + '"'
    text = (
        '2026-05-28 * "Lot roundtrip"\n'
        '  cashier_id: "lot-roundtrip"\n'
        f'  Assets:Cash 7.123456789 USD {{1 USD, {quoted}}}\n'
        '  Equity:Opening-Balances -7.123456789 USD\n'
    )
    entries, errors, _ = writeback._parse_transaction(text)
    assert not errors
    entry = entries[0]
    if not preserves_semantics:
        with pytest.raises(ValueError, match="serialization must preserve"):
            writeback._normalize_entry(entry)
        with pytest.raises(ValueError, match="serialization must preserve"):
            writeback._append_entries_to_source("", [entry])
        return
    rendered = writeback._normalize_entry(entry)
    reparsed, errors, _ = writeback._parse_transaction(rendered)
    assert not errors
    assert len(reparsed) == 1
    assert writeback._transaction_semantics(reparsed[0]) == writeback._transaction_semantics(entry)
    assert reparsed[0].postings[0].cost.label == label
    assert reparsed[0].postings[0].units.number == Decimal("7.123456789")


@pytest.mark.parametrize("label", ["ordinary lot", "first line\nsecond line"])
def test_safe_lot_labels_remain_capturable_and_idempotent(configured_env, label):
    from cashier_snapshot.workspace import LedgerWorkspace

    text = (
        '2026-05-28 * "Lot roundtrip"\n'
        '  cashier_id: "lot-roundtrip"\n'
        f'  Assets:Cash 7.123456789 USD {{1 USD, "{label}"}}\n'
        '  Equity:Opening-Balances -7.123456789 USD\n'
    )
    result, wrote = writeback.validate_and_commit([text])
    assert wrote
    assert result == {"synchronized": ["lot-roundtrip"], "rejected": []}
    manual = Path(configured_env)
    committed = manual.read_bytes()
    workspace = LedgerWorkspace.capture(manual.with_name("main.bean"))
    assert workspace.files[Path(manual.name)] == committed
    reparsed, errors, _ = writeback._parse_transaction(committed.decode())
    assert not errors
    assert len(reparsed) == 1
    assert reparsed[0].postings[0].cost.label == label
    assert reparsed[0].postings[0].units.number == Decimal("7.123456789")
    retry, wrote = writeback.validate_and_commit([text])
    assert not wrote
    assert retry == result
    assert manual.read_bytes() == committed
