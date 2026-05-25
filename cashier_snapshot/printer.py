"""Print materialized PWA-only entries as parseable textual Beancount."""

from __future__ import annotations

import datetime
import enum
import re
from decimal import Decimal
from io import StringIO

from beancount.core import amount, data, inventory
from beancount.parser import printer


TEXTUAL_METADATA_KEY_RE = re.compile(r"^[a-z][A-Za-z0-9_-]*$")


def pwa_metadata_key(key: str, existing_keys: set[str]) -> str:
    """Return a textual-Beancount-compatible metadata key for PWA export."""
    if TEXTUAL_METADATA_KEY_RE.match(key) and not key.startswith("_"):
        return key

    safe_suffix = re.sub(r"[^A-Za-z0-9_-]", "_", key.lstrip("_"))
    safe_suffix = re.sub(r"^[^A-Za-z]+", "", safe_suffix) or "metadata"
    base = f"pwa_{safe_suffix}"
    candidate = base
    suffix = 2
    while candidate in existing_keys:
        candidate = f"{base}_{suffix}"
        suffix += 1
    return candidate


def canonicalize_metadata_value_for_pwa(value):
    """Return a metadata value that Beancount's text parser can read back."""
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, (str, Decimal, int, float, datetime.date, amount.Amount, bool)) or value is None:
        return value
    if isinstance(value, (inventory.Inventory, data.Cost, data.CostSpec)):
        return str(value)
    return str(value)


def canonicalize_metadata_for_pwa(meta: dict) -> dict:
    """Preserve metadata while making keys and values printable/parseable."""
    canonical_meta = {}
    reserved_keys = {
        key
        for key in meta
        if TEXTUAL_METADATA_KEY_RE.match(key) and not key.startswith("_")
    }
    for key, value in meta.items():
        canonical_key = pwa_metadata_key(key, reserved_keys | set(canonical_meta.keys()))
        canonical_meta[canonical_key] = canonicalize_metadata_value_for_pwa(value)
    return canonical_meta


def canonicalize_entry_metadata_for_pwa(entry):
    """Canonicalize metadata on an entry and, for transactions, its postings."""
    canonical_entry = entry._replace(meta=canonicalize_metadata_for_pwa(entry.meta))
    if isinstance(canonical_entry, data.Transaction):
        canonical_postings = [
            posting._replace(meta=canonicalize_metadata_for_pwa(posting.meta or {}))
            for posting in canonical_entry.postings
        ]
        canonical_entry = canonical_entry._replace(postings=canonical_postings)
    return canonical_entry


def canonicalize_materialized_entries_for_pwa(entries: list) -> list:
    """Normalize generated entries for a second, textual PWA parse.

    Consumed ``Pad`` directives are omitted because concrete padding transactions
    already represent their executed result. Python-only metadata is converted
    to textual Beancount-compatible keys and values.
    """
    return [
        canonicalize_entry_metadata_for_pwa(entry)
        for entry in entries
        if not isinstance(entry, data.Pad)
    ]


def print_materialized_entries(entries: list, options_map: dict) -> str:
    """Serialize materialized Beancount entries to parseable text."""
    output = StringIO()
    eprinter = printer.EntryPrinter(
        dcontext=options_map.get("dcontext"),
        stringify_invalid_types=True,
    )
    previous_type = type(entries[0]) if entries else None

    for entry in entries:
        entry_type = type(entry)
        if entry_type in (printer.data.Transaction, printer.data.Commodity) or entry_type is not previous_type:
            output.write("\n")
            previous_type = entry_type
        output.write(eprinter(entry))

    return output.getvalue()


def print_generated_entries_for_pwa(entries: list, options_map: dict) -> str:
    """Print plugin-generated entries after normalizing Python-only metadata."""
    canonical_entries = canonicalize_materialized_entries_for_pwa(entries)
    return print_materialized_entries(canonical_entries, options_map)
