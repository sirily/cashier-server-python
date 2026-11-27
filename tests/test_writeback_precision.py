"""Writeback must not use a ledger's display precision as storage precision."""
from decimal import Decimal
from pathlib import Path

import pytest
from beancount import loader
from beancount.core import data
from beancount.parser import parser

import writeback
from tests.test_writeback import client


@pytest.fixture
def fx_ledger(configured_env):
    manual = Path(configured_env)
    with manual.with_name("main.bean").open("a", encoding="utf-8") as root:
        root.write("2024-01-01 open Assets:Foreign GBP\n")
    return manual


@pytest.mark.parametrize("postings,annotation", [
    ("  Assets:Foreign -3 GBP @@ 1.00 USD\n  Assets:Cash 1.00 USD\n", "@@ 1.00 USD"),
    (("  Assets:Foreign -3 GBP @@ 0.1234567890123456789012345678 USD\n"
      "  Assets:Cash 0.1234567890123456789012345678 USD\n"), "@@ 0.1234567890123456789012345678 USD"),
    ("  Assets:Foreign 3 GBP @@ 1.00 USD\n  Assets:Cash -1.00 USD\n", "@@ 1.00 USD"),
    ("  Assets:Foreign -3 GBP @ 0.25 USD\n  Assets:Cash 0.75 USD\n", "@ 0.25 USD"),
    ("  Assets:Foreign -3 GBP @@ 0 USD\n  Assets:Cash 0 USD\n", "@@ 0 USD"),
    ("  Assets:Cash -3.00 USD\n  Equity:Opening-Balances 3.00 USD\n", None),
])
@pytest.mark.parametrize("same_batch", [False, True])
def test_price_notation_survives_writeback_pull_and_retry(fx_ledger, postings, annotation, same_batch):
    text = '2026-05-28 * "Transfer"\n  cashier_id: "exact-price"\n' + postings
    response = client.post("/xact", json={"transactions": [text, text] if same_batch else [text]})
    assert response.status_code == 200
    assert response.json() == {"synchronized": ["exact-price"], "rejected": []}
    committed = fx_ledger.read_bytes()

    raw = client.get("/infrastructure", params={"file_path": "manual_transactions.bean"})
    snapshot = client.get("/infrastructure", params={"file_path": "main.bean"})
    assert raw.status_code == 403
    assert snapshot.status_code == 200
    assert raw.json() == {"detail": "Only the configured root snapshot is available"}
    for content in (committed.decode(), snapshot.json()["content"]):
        assert content.count('cashier_id: "exact-price"') == 1
        assert "__cashier" not in content
        if annotation is None:
            assert "@" not in content
        else:
            assert annotation in content
            if not annotation.startswith("@@"):
                assert "@@" not in content
            else:
                assert " @ " not in content
        entries, errors, _ = parser.parse_string(content)
        assert not errors
        matching = [
            entry for entry in entries
            if isinstance(entry, data.Transaction) and entry.meta.get("cashier_id") == "exact-price"
        ]
        assert len(matching) == 1

    _, errors, _ = loader.load_string(snapshot.json()["content"])
    assert not errors
    retry = client.post("/xact", json={"transactions": [text]})
    assert retry.status_code == 200
    assert retry.json() == response.json()
    assert fx_ledger.read_bytes() == committed
    pulled_again = client.get("/infrastructure", params={"file_path": "main.bean"})
    assert pulled_again.status_code == 200
    assert pulled_again.json()["content"].count('cashier_id: "exact-price"') == 1


@pytest.mark.parametrize("same_batch", [False, True])
@pytest.mark.parametrize("first_price,second_price", [
    ("@@ 1.00000000000000000000000000001 USD", "@@ 1.00000000000000000000000000002 USD"),
    ("@@ 1.00 USD", "@ 0.3333333333333333333333333333 USD"),
    ("@ 0.3333333333333333333333333333 USD", "@@ 1.00 USD"),
])
def test_exact_price_conflicts_even_when_parsed_unit_prices_match(fx_ledger, same_batch, first_price, second_price):
    template = (
        '2026-05-28 * "Transfer"\n'
        '  cashier_id: "price-conflict"\n'
        '  Assets:Foreign -3 GBP {price}\n'
        '  Assets:Cash 1.00 USD\n'
    )
    first, second = [template.format(price=price) for price in (first_price, second_price)]
    parsed_first, errors, _ = parser.parse_string(first)
    assert not errors
    parsed_second, errors, _ = parser.parse_string(second)
    assert not errors
    assert isinstance(parsed_first[0], data.Transaction)
    assert isinstance(parsed_second[0], data.Transaction)
    assert parsed_first[0].postings[0].price == parsed_second[0].postings[0].price
    if same_batch:
        response = client.post("/xact", json={"transactions": [first, second]})
    else:
        assert client.post("/xact", json={"transactions": [first]}).json()["rejected"] == []
        before = fx_ledger.read_bytes()
        response = client.post("/xact", json={"transactions": [second]})
        assert fx_ledger.read_bytes() == before
    assert response.status_code == 200
    assert len(response.json()["rejected"]) == 1
    assert "Conflict" in response.json()["rejected"][0]["reason"]
    assert first_price in fx_ledger.read_text()
    assert fx_ledger.read_text().count('cashier_id: "price-conflict"') == 1


@pytest.mark.parametrize("alteration", ["total", "currency", "notation"])
def test_total_price_serialization_is_semantically_checked(monkeypatch, alteration):
    text = (
        '2026-05-28 * "Transfer"\n'
        '  cashier_id: "exact-price"\n'
        '  Assets:Foreign -3 GBP @@ 1.00000000000000000000000000001 USD\n'
        '  Assets:Cash 1.00 USD\n'
    )
    entries, errors, _ = writeback._parse_transaction(text)
    assert not errors
    original = writeback._WritebackPrinter

    class ChangedPrinter(original):
        def __call__(self, entry):
            rendered = super().__call__(entry)
            if alteration == "total":
                return rendered.replace("1.00000000000000000000000000001", "1.00000000000000000000000000002")
            if alteration == "currency":
                return rendered.replace("00001 USD", "00001 EUR")
            return rendered.replace("@@ 1.00000000000000000000000000001", "@ 0.3333333333333333333333333333")

    monkeypatch.setattr(writeback, "_WritebackPrinter", ChangedPrinter)
    with pytest.raises(ValueError, match="serialization must preserve"):
        writeback._normalize_entry(entries[0])


def test_total_prices_and_unit_prices_on_multiple_postings_roundtrip():
    text = (
        '2026-05-28 * "Transfer"\n'
        '  cashier_id: "multiple-prices"\n'
        '  Assets:Foreign -3 GBP @@ 1.00 USD\n'
        '    note: "First @@ is total"\n'
        '  Assets:Foreign -7 GBP @ 0.25 USD\n'
        '  Assets:Foreign -9 GBP {1 USD} @@ 2.00 USD\n'
        '    note: "Second @@ is total"\n'
        '  Assets:Cash 4.75 USD\n'
    )
    entries, errors, _ = writeback._parse_transaction(text)
    assert not errors
    rendered = writeback._normalize_entry(entries[0])
    assert "@@ 1.00 USD" in rendered
    assert "@@ 2.00 USD" in rendered
    assert "@ 0.25 USD" in rendered
    assert "__cashier" not in rendered
    reparsed, errors, _ = writeback._parse_transaction(rendered)
    assert not errors
    assert writeback._transaction_semantics(reparsed[0]) == writeback._transaction_semantics(entries[0])


def test_existing_total_price_source_and_controls_survive_retry(fx_ledger):
    text = (
        '2026-05-28 * "Transfer"\n'
        '  cashier_id: "existing-price"\n'
        '  Assets:Foreign -3 GBP @@ 1.00 USD\n'
        '  Assets:Cash 1.00 USD\n'
    )
    previous = ('option "title" "Manual transactions"\n' + text).encode()
    fx_ledger.write_bytes(previous)
    result, wrote = writeback.validate_and_commit([text])
    assert not wrote
    assert result == {"synchronized": ["existing-price"], "rejected": []}
    assert fx_ledger.read_bytes() == previous


@pytest.mark.parametrize("posting", [
    "Assets:Foreign -3 GBP @@ -1.00 USD",
    "Assets:Foreign GBP @@ 1.00 USD",

])
def test_invalid_total_price_does_not_write(fx_ledger, posting):
    text = (
        '2026-05-28 * "Invalid transfer"\n'
        '  cashier_id: "invalid-price"\n'
        f'  {posting}\n'
        '  Assets:Cash 1.00 USD\n'
    )
    result, wrote = writeback.validate_and_commit([text])
    assert not wrote
    assert result["synchronized"] == []
    assert len(result["rejected"]) == 1
    assert fx_ledger.read_bytes() == b""


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
