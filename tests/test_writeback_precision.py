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
