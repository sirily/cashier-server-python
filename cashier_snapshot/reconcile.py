"""Reconcile materialized Beancount entries with their source directives."""

from __future__ import annotations

import re

from beancount.core import data
from beancount.core.number import MISSING

from .errors import SnapshotBuildError
from .source_index import SourceBlock


LOSSY_SOURCE_SYNTAX_RE = re.compile(r"(?:\s@@?\s|\{\{|\{[^\n}]*#)")


def _public_metadata(meta: dict | None) -> dict:
    return {
        key: value
        for key, value in (meta or {}).items()
        if key not in {"filename", "lineno"} and not key.startswith("_")
    }


def _source_semantics(entry):
    """Ignore loader/plugin runtime metadata, never user-visible entry content."""
    clean_entry = entry._replace(meta=_public_metadata(entry.meta))
    if isinstance(clean_entry, data.Transaction):
        clean_postings = [
            posting._replace(meta=_public_metadata(posting.meta))
            for posting in clean_entry.postings
        ]
        clean_entry = clean_entry._replace(postings=clean_postings)
    return clean_entry


def _is_empty_cost_spec(cost) -> bool:
    """Return True for an empty/unspecified CostSpec (``{}`` syntax).

    The Python Beancount parser produces this form before FIFO booking
    populates the concrete cost.  It is safe to retain the original source
    text because no explicit cost semantics are erased.
    """
    return (
        isinstance(cost, data.CostSpec)
        and cost.number_per is MISSING
        and cost.number_total is None
        and cost.currency is MISSING
        and cost.date is None
        and cost.label is None
        and cost.merge is False
    )


def _posting_matches_booking_baseline(source_posting, booked_posting) -> bool:
    """Allow only parser-to-core-booking changes that preserve raw source output.

    Beancount resolves an empty cost spec (``{}``) during core booking.  That
    change is safe to keep in raw source; it must not be used when comparing the
    later plugin-materialized result with this booked baseline.
    """
    if source_posting.account != booked_posting.account:
        return False
    if source_posting.units is not MISSING and source_posting.units != booked_posting.units:
        return False
    if _is_empty_cost_spec(source_posting.cost):
        if source_posting.price != booked_posting.price:
            return False
    elif source_posting.cost != booked_posting.cost or source_posting.price != booked_posting.price:
        return False
    if source_posting.flag != booked_posting.flag:
        return False
    return _public_metadata(source_posting.meta) == _public_metadata(booked_posting.meta)


def _source_matches_booking_baseline(source_entry, booked_entry) -> bool:
    """Whether raw source faithfully represents the core-booked baseline."""
    source = _source_semantics(source_entry)
    booked = _source_semantics(booked_entry)
    if isinstance(source, data.Transaction) and isinstance(booked, data.Transaction):
        return (
            source.date == booked.date
            and source.flag == booked.flag
            and source.payee == booked.payee
            and source.narration == booked.narration
            and source.tags == booked.tags
            and source.links == booked.links
            and source.meta == booked.meta
            and len(source.postings) == len(booked.postings)
            and all(
                _posting_matches_booking_baseline(source_posting, booked_posting)
                for source_posting, booked_posting in zip(source.postings, booked.postings)
            )
        )
    return source == booked


def _booking_baseline_is_unchanged(booked_entry, final_entry) -> bool:
    """Require exact semantic equality after booking so plugins cannot hide edits."""
    return _source_semantics(booked_entry) == _source_semantics(final_entry)


def _safe_printable_transform(source_block: SourceBlock, final_entry) -> bool:
    """Print transformations only when no erased source price/cost syntax exists.

    Python Beancount normalizes total-price (``@@``) and total-cost forms
    (``{{ ... }}`` / ``{ ... # ... }``) when it parses source. Reprinting a
    plugin-transformed directive from the parsed object may therefore alter its
    exact accounting meaning, so those forms remain fail-closed.
    """
    if LOSSY_SOURCE_SYNTAX_RE.search(source_block.text):
        return False
    return type(source_block.entry) is type(final_entry)


def final_origin_key(entry) -> tuple[str, int, str]:
    return (
        entry.meta.get("filename"),
        entry.meta.get("lineno"),
        type(entry).__name__,
    )


def classify_source_entries(
    entries: list,
    entry_blocks: dict[tuple[str, int, str], SourceBlock],
    booked_entries_by_origin: dict[tuple[str, int, str], object],
) -> tuple[set[int], set[int]]:
    """Return materialized entry indices safe to retain verbatim or print changed.

    Raw retention crosses two distinct boundaries: source syntax may differ from
    Beancount's core-booked baseline (for example ``{}`` resolving to a FIFO
    lot), but the later plugin-materialized entry must equal that baseline.
    """
    final_by_origin: dict[tuple[str, int, str], list[tuple[int, object]]] = {}
    for index, entry in enumerate(entries):
        final_by_origin.setdefault(final_origin_key(entry), []).append((index, entry))

    retained_indices: set[int] = set()
    transformed_indices: set[int] = set()
    for origin_key, source_block in entry_blocks.items():
        if isinstance(source_block.entry, data.Pad):
            continue
        candidates = final_by_origin.get(origin_key, [])
        booked_entry = booked_entries_by_origin.get(origin_key)
        retained_index = next(
            (
                index
                for index, final_entry in candidates
                if booked_entry is not None
                and _source_matches_booking_baseline(source_block.entry, booked_entry)
                and _booking_baseline_is_unchanged(booked_entry, final_entry)
            ),
            None,
        )
        if retained_index is not None:
            retained_indices.add(retained_index)
            continue

        transformed_index = next(
            (
                index
                for index, final_entry in candidates
                if _safe_printable_transform(source_block, final_entry)
            ),
            None,
        )
        if transformed_index is not None:
            transformed_indices.add(transformed_index)
            continue
        raise SnapshotBuildError(
            "Standalone snapshot cannot safely export transformed source directive: "
            f"{source_block.relative_path}:{source_block.lineno} "
            f"({type(source_block.entry).__name__})"
        )

    return retained_indices, transformed_indices

    return retained_indices, transformed_indices
