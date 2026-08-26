"""Read Beancount source blocks before Python plugins erase textual semantics."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from beancount.parser import parser

from .errors import SnapshotBuildError


DIRECTIVE_START_RE = re.compile(
    r"^(?:"
    r"\d{4}-\d{2}-\d{2}(?=\s|[*!&#?%PSTC])\s*|"
    r"option\s+|include\s+|plugin\s+|"
    r"pushtag\s+|poptag\s+|pushmeta\s+|popmeta\s+"
    r")"
)
INCLUDE_RE = re.compile(r'^include\s+"([^"]+)"')
PLUGIN_RE = re.compile(r'^plugin\s+"([^"]+)"')


@dataclass(frozen=True)
class SourceBlock:
    """A textual source directive with its parsed entry, if it creates one."""

    path: Path
    relative_path: str
    lineno: int
    kind: str
    text: str
    entry: object | None = None
    plugin_name: str | None = None

    @property
    def origin_key(self) -> tuple[str, int, str] | None:
        if self.entry is None:
            return None
        return (str(self.path), self.lineno, type(self.entry).__name__)


class SourceLedgerIndex:
    """Flatten a Beancount workspace while preserving each source block verbatim."""

    def __init__(self, root_path: Path):
        self.root_path = root_path.resolve()
        self.ledger_root = self.root_path.parent
        self.blocks: list[SourceBlock] = []
        self.plugin_names: set[str] = set()
        self._active_stack: list[Path] = []

    @classmethod
    def build(cls, root_path: Path) -> "SourceLedgerIndex":
        index = cls(root_path)
        index._index_file(index.root_path)
        return index

    @property
    def entry_blocks(self) -> dict[tuple[str, int, str], SourceBlock]:
        entry_blocks: dict[tuple[str, int, str], SourceBlock] = {}
        for block in self.blocks:
            if block.origin_key is None:
                continue
            if block.origin_key in entry_blocks:
                raise SnapshotBuildError(
                    "Repeated included source directive is not safely exportable: "
                    f"{block.relative_path}:{block.lineno}"
                )
            entry_blocks[block.origin_key] = block
        return entry_blocks

    def _ensure_inside_ledger_root(self, path: Path) -> Path:
        if path.is_symlink():
            raise SnapshotBuildError(f"Symlink source include is not exportable: {path}")
        resolved_path = path.resolve()
        try:
            resolved_path.relative_to(self.ledger_root)
        except ValueError as exc:
            raise SnapshotBuildError(
                f"Included source file escapes ledger root: {path}"
            ) from exc
        return resolved_path

    def _include_paths(self, current_path: Path, include_pattern: str) -> list[Path]:
        requested_path = Path(include_pattern)
        if requested_path.is_absolute() or ".." in requested_path.parts:
            raise SnapshotBuildError(f"Unsafe source include path: {include_pattern}")

        matches = sorted(current_path.parent.glob(include_pattern))
        if not matches:
            raise SnapshotBuildError(f"Included source file not found: {include_pattern}")
        paths = []
        for match in matches:
            resolved = self._ensure_inside_ledger_root(match)
            if not resolved.is_file():
                raise SnapshotBuildError(f"Included source path is not a file: {include_pattern}")
            paths.append(resolved)
        return paths

    def _parse_entries(self, path: Path) -> dict[int, object]:
        entries, errors, _ = parser.parse_file(str(path))
        if errors:
            raise SnapshotBuildError(
                f"Source file could not be indexed: {path.name}: "
                + "; ".join(str(error) for error in errors)
            )
        return {entry.meta["lineno"]: entry for entry in entries}

    def _split_blocks(self, path: Path) -> list[tuple[int, str]]:
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        starts = [
            line_number
            for line_number, line in enumerate(lines, start=1)
            if DIRECTIVE_START_RE.match(line)
        ]
        blocks: list[tuple[int, str]] = []
        if starts and starts[0] > 1:
            blocks.append((1, "".join(lines[: starts[0] - 1])))
        elif not starts and lines:
            blocks.append((1, "".join(lines)))

        for index, start in enumerate(starts):
            end = starts[index + 1] - 1 if index + 1 < len(starts) else len(lines)
            blocks.append((start, "".join(lines[start - 1 : end])))
        return blocks

    def _index_file(self, path: Path) -> None:
        path = self._ensure_inside_ledger_root(path)
        if path in self._active_stack:
            raise SnapshotBuildError(f"Cyclic include in ledger source: {path.name}")

        self._active_stack.append(path)
        try:
            relative_path = path.relative_to(self.ledger_root).as_posix()
            entries_by_line = self._parse_entries(path)
            for lineno, text in self._split_blocks(path):
                stripped = text.lstrip()
                include_match = INCLUDE_RE.match(stripped)
                plugin_match = PLUGIN_RE.match(stripped)

                if include_match:
                    self.blocks.append(
                        SourceBlock(path, relative_path, lineno, "include", text)
                    )
                    for included_path in self._include_paths(path, include_match.group(1)):
                        self._index_file(included_path)
                    continue

                if plugin_match:
                    plugin_name = plugin_match.group(1)
                    self.plugin_names.add(plugin_name)
                    self.blocks.append(
                        SourceBlock(
                            path,
                            relative_path,
                            lineno,
                            "plugin",
                            text,
                            plugin_name=plugin_name,
                        )
                    )
                    continue

                entry = entries_by_line.get(lineno)
                if entry is not None:
                    self.blocks.append(
                        SourceBlock(path, relative_path, lineno, "entry", text, entry=entry)
                    )
                    continue

                if (
                    stripped.startswith("option ")
                    or not stripped
                    or stripped.startswith(";")
                    or stripped.startswith("*")
                ):
                    self.blocks.append(
                        SourceBlock(path, relative_path, lineno, "header", text)
                    )
                    continue

                raise SnapshotBuildError(
                    f"Unsupported non-entry source directive at {relative_path}:{lineno}"
                )
        finally:
            self._active_stack.pop()
