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


def _posting_matches_source(source_posting, final_posting) -> bool:
    if source_posting.account != final_posting.account:
        return False
    if source_posting.units is not MISSING and source_posting.units != final_posting.units:
        return False
    if source_posting.cost != final_posting.cost or source_posting.price != final_posting.price:
        return False
    if source_posting.flag != final_posting.flag:
        return False
    return _public_metadata(source_posting.meta) == _public_metadata(final_posting.meta)


def _source_entry_is_retained(source_entry, final_entry) -> bool:
    source = _source_semantics(source_entry)
    final = _source_semantics(final_entry)
    if isinstance(source, data.Transaction) and isinstance(final, data.Transaction):
        return (
            source.date == final.date
            and source.flag == final.flag
            and source.payee == final.payee
            and source.narration == final.narration
            and source.tags == final.tags
            and source.links == final.links
            and source.meta == final.meta
            and len(source.postings) == len(final.postings)
            and all(
                _posting_matches_source(source_posting, final_posting)
                for source_posting, final_posting in zip(source.postings, final.postings)
            )
        )
    return source == final


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
) -> tuple[set[int], set[int]]:
    """Return materialized entry indices safe to retain verbatim or print changed."""
    final_by_origin: dict[tuple[str, int, str], list[tuple[int, object]]] = {}
    for index, entry in enumerate(entries):
        final_by_origin.setdefault(final_origin_key(entry), []).append((index, entry))

    retained_indices: set[int] = set()
    transformed_indices: set[int] = set()
    for origin_key, source_block in entry_blocks.items():
        if isinstance(source_block.entry, data.Pad):
            continue
        candidates = final_by_origin.get(origin_key, [])
        retained_index = next(
            (
                index
                for index, final_entry in candidates
                if _source_entry_is_retained(source_block.entry, final_entry)
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
            "Standalone snapshot cannot safely export transformed source "
            f"directive: {source_block.relative_path}:{source_block.lineno} "
            f"({type(source_block.entry).__name__})"
        )

    return retained_indices, transformed_indices
