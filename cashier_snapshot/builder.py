"""Build a standalone ledger without round-tripping unchanged source entries."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from beancount import loader
from beancount.core import data

from .booking_baseline import load_booked_source_entries
from .errors import SnapshotBuildError
from .plugin_policies import validate_source_preserving_plugins
from .reconcile import classify_source_entries, final_origin_key
from .source_index import SourceLedgerIndex


@dataclass
class SnapshotStats:
    retained: int = 0
    omitted_infrastructure: int = 0
    consumed_operational: int = 0
    generated: int = 0
    transformed: int = 0


def append_snapshot_block(output: list[str], text: str) -> None:
    """Append a ledger block without concatenating adjacent directives."""
    if output and output[-1] and not output[-1].endswith("\n"):
        output.append("\n")
    output.append(text)


class StandaloneSnapshotBuilder:
    """Coordinate source indexing, plugin materialization and snapshot output."""

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

        booked_entries, booking_errors, _ = load_booked_source_entries(self.root_path)
        if booking_errors:
            raise SnapshotBuildError(
                "Beancount root book could not be core-booked before materialization: "
                + "; ".join(str(error) for error in booking_errors)
            )
        booked_entries_by_origin = {
            final_origin_key(entry): entry for entry in booked_entries
        }

        entries, errors, options_map = loader.load_file(str(self.root_path))
        if errors:
            raise SnapshotBuildError(
                "Beancount root book could not be materialized: "
                + "; ".join(str(error) for error in errors)
            )

        entry_blocks = {
            block.origin_key: block
            for block in source_index.blocks
            if block.origin_key is not None
        }
        retained_indices, transformed_indices = classify_source_entries(
            entries, entry_blocks, booked_entries_by_origin
        )
        stats = SnapshotStats(
            omitted_infrastructure=sum(
                block.kind in {"plugin", "include"} for block in source_index.blocks
            )
        )

        output: list[str] = []
        for block in source_index.blocks:
            if block.kind == "header":
                append_snapshot_block(output, block.text)

        for index, entry in enumerate(entries):
            source_block = entry_blocks.get(final_origin_key(entry))
            if isinstance(entry, data.Pad):
                stats.consumed_operational += 1
                continue
            if index in retained_indices:
                assert source_block is not None
                append_snapshot_block(output, source_block.text)
                stats.retained += 1
                continue
            append_snapshot_block(output, self.print_generated([entry], options_map))
            if index in transformed_indices:
                stats.transformed += 1
            else:
                stats.generated += 1

        return "".join(output), stats
