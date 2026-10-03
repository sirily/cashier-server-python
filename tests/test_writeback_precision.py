"""Writeback must not use a ledger's display precision as storage precision."""
from decimal import Decimal
from pathlib import Path

import pytest
from beancount.core import display_context
from beancount.parser import parser

import writeback
from tests.test_writeback import client, configured_env


def transaction(number="7.25"):
    return (
        '2026-05-28 * "Decimal"\n'
        '  cashier_id: "decimal-regression"\n'
        f'  Assets:Cash -{number} USD\n'
        f'  Equity:Opening-Balances {number} USD\n'
    )


def test_post_preserves_decimal_amounts(configured_env):
    response = client.post("/xact", json={"transactions": [transaction()]})
    assert response.json() == {"synchronized": ["decimal-regression"], "rejected": []}
    entries, errors, _ = parser.parse_string(Path(configured_env).read_text())
    assert not errors
    assert [p.units.number for p in entries[0].postings] == [Decimal("-7.25"), Decimal("7.25")]
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
    context = display_context.DisplayContext()
    context.update(Decimal("7"), "USD")
    text = (
        '2026-05-28 * "Precision"\n'
        '  cashier_id: "precision"\n'
        f'  Assets:Cash 7.123456789 USD{annotation}\n'
        '  Equity:Opening-Balances -7.123456789 USD\n'
    )
    entries, errors, _ = parser.parse_string(text)
    assert not errors
    options = {"dcontext": context}
    for rendered in (writeback._normalize_entry(entries[0], options), writeback._append_entries_to_source("; untouched\n", entries, options)):
        reparsed, errors, _ = parser.parse_string(rendered)
        assert not errors
        assert [p._replace(meta=None) for p in reparsed[0].postings] == [p._replace(meta=None) for p in entries[0].postings]
    changed, errors, _ = parser.parse_string(text.replace("123456789", "123456788"))
    assert not errors
    assert writeback._normalize_entry(entries[0], options) != writeback._normalize_entry(changed[0], options)
