"""Build a standalone ledger without round-tripping unchanged source entries."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from beancount import loader
from beancount.core import data
from beancount.core.number import MISSING

from .errors import SnapshotBuildError
from .plugin_policies import validate_source_preserving_plugins
from .source_index import SourceBlock, SourceLedgerIndex


PRICE_SYNTAX_RE = re.compile(r"\s@@?\s")


@dataclass
class SnapshotStats:
    retained: int = 0
    omitted_infrastructure: int = 0
    consumed_operational: int = 0
    generated: int = 0
    transformed: int = 0


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
    """Print known transformations only when no erased source price syntax exists."""
    if PRICE_SYNTAX_RE.search(source_block.text):
        return False
    return type(source_block.entry) is type(final_entry)


def _final_origin_key(entry) -> tuple[str, int, str]:
    return (
        entry.meta.get("filename"),
        entry.meta.get("lineno"),
        type(entry).__name__,
    )


class StandaloneSnapshotBuilder:
    """Reconcile materialized entries with source text and produce PWA input."""

    def __init__(
        self,
        root_path: Path,
        print_generated: Callable[[list, dict], str],
    ):
        self.root_path = root_path.resolve()
        self.print_generated = print_generated

    def build(self) -> tuple[str, SnapshotStats]:
        source_index = SourceLedgerIndex.build(self.root_path)
        validate_source_preserving_plugins(source_index.plugin_names)

        entries, errors, options_map = loader.load_file(str(self.root_path))
        if errors:
            raise SnapshotBuildError(
                "Beancount root book could not be materialized: "
                + "; ".join(str(error) for error in errors)
            )

        entry_blocks = source_index.entry_blocks
        retained_indices, transformed_indices = self._classified_source_indices(
            entries, entry_blocks
        )
        stats = SnapshotStats(
            omitted_infrastructure=sum(
                block.kind in {"plugin", "include"} for block in source_index.blocks
            )
        )

        output: list[str] = []
        for block in source_index.blocks:
            if block.kind == "header":
                output.append(block.text)

        for index, entry in enumerate(entries):
            source_block = entry_blocks.get(_final_origin_key(entry))
            if isinstance(entry, data.Pad):
                stats.consumed_operational += 1
                continue
            if index in retained_indices:
                assert source_block is not None
                output.append(source_block.text)
                stats.retained += 1
                continue
            output.append(self.print_generated([entry], options_map))
            if index in transformed_indices:
                stats.transformed += 1
            else:
                stats.generated += 1

        return "".join(output), stats

    def _classified_source_indices(
        self,
        entries: list,
        entry_blocks: dict[tuple[str, int, str], SourceBlock],
    ) -> tuple[set[int], set[int]]:
        final_by_origin: dict[tuple[str, int, str], list[tuple[int, object]]] = {}
        for index, entry in enumerate(entries):
            final_by_origin.setdefault(_final_origin_key(entry), []).append((index, entry))

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
