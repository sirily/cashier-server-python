"""Read Beancount source blocks before Python plugins erase textual semantics."""

from __future__ import annotations

import re
from dataclasses import dataclass
from os.path import abspath
from pathlib import Path

from beancount.parser import lexer, parser

from .errors import SnapshotBuildError
from .workspace import LedgerWorkspace, LedgerWorkspaceError

DIRECTIVE_TOKENS = {
    "DATE",
    "OPTION",
    "INCLUDE",
    "PLUGIN",
    "PUSHTAG",
    "POPTAG",
    "PUSHMETA",
    "POPMETA",
}
INCLUDE_RE = re.compile(r"^include\s+")
PLUGIN_RE = re.compile(r"^plugin\s+")


@dataclass(frozen=True)
class SourceBlock:
    """A textual source directive with its parsed entry, if it creates one."""

    path: Path
    relative_path: str
    lineno: int
    kind: str
    text: str
    entry: object | None = None

    @property
    def origin_key(self) -> tuple[str, int, str] | None:
        if self.entry is None:
            return None
        return (str(self.path), self.lineno, type(self.entry).__name__)


class SourceLedgerIndex:
    """Flatten a Beancount workspace while preserving each source block verbatim."""

    def __init__(self, root_path: Path, workspace: LedgerWorkspace):
        self.root_path = Path(abspath(root_path))
        self.workspace = workspace
        self.ledger_root = self.root_path.parent
        self.blocks: list[SourceBlock] = []
        self.plugin_names: set[str] = set()
        self._active_stack: list[Path] = []

    @classmethod
    def build(
        cls, root_path: Path, *, workspace: LedgerWorkspace | None = None
    ) -> SourceLedgerIndex:
        try:
            index = cls(root_path, workspace or LedgerWorkspace.capture(root_path))
            index._index_file(index.root_path)
            return index
        except LedgerWorkspaceError as exc:
            raise SnapshotBuildError(str(exc)) from exc

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

    def _include_paths(self, current_path: Path, include_pattern: str) -> list[Path]:
        relative = current_path.relative_to(self.ledger_root)
        return [
            self.ledger_root / included
            for included in self.workspace._included_paths(relative, include_pattern)
        ]

    def _parse_entries(self, path: Path) -> tuple[dict[int, object], dict]:
        entries, errors, options = parser.parse_string(
            self.workspace._files[path.relative_to(self.ledger_root)],
            report_filename=str(path),
        )
        if errors:
            raise SnapshotBuildError(
                f"Source file could not be indexed: {path.name}: "
                + "; ".join(str(error) for error in errors)
            )
        return {entry.meta["lineno"]: entry for entry in entries}, options

    def _split_blocks(self, path: Path) -> list[tuple[int, str]]:
        source = self.workspace._files[path.relative_to(self.ledger_root)]
        lines = source.decode("utf-8").splitlines(keepends=True)
        starts = []
        at_line_start = True
        # The lexer keeps directive-looking text inside multiline strings opaque.
        for token, lineno, _, _ in lexer.lex_iter_string(source):
            if token == "EOL":
                at_line_start = True
            elif at_line_start:
                at_line_start = False
                if token in DIRECTIVE_TOKENS:
                    starts.append(lineno)
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
        if path in self._active_stack:
            raise SnapshotBuildError(f"Cyclic include in ledger source: {path.name}")

        self._active_stack.append(path)
        try:
            relative_path = path.relative_to(self.ledger_root).as_posix()
            entries_by_line, options = self._parse_entries(path)
            includes = iter(options["include"])
            plugins = iter(options["plugin"])
            for lineno, text in self._split_blocks(path):
                stripped = text.lstrip()
                include_match = INCLUDE_RE.match(stripped)
                plugin_match = PLUGIN_RE.match(stripped)

                if include_match:
                    self.blocks.append(
                        SourceBlock(path, relative_path, lineno, "include", text)
                    )
                    for included_path in self._include_paths(path, next(includes)):
                        self._index_file(included_path)
                    continue

                if plugin_match:
                    plugin_name, _ = next(plugins)
                    self.plugin_names.add(plugin_name)
                    self.blocks.append(
                        SourceBlock(
                            path,
                            relative_path,
                            lineno,
                            "plugin",
                            text,
                        )
                    )
                    continue

                entry = entries_by_line.get(lineno)
                if entry is not None:
                    self.blocks.append(
                        SourceBlock(
                            path, relative_path, lineno, "entry", text, entry=entry
                        )
                    )
                    continue

                if stripped.startswith(("option ", ";", "*")) or not stripped:
                    self.blocks.append(
                        SourceBlock(path, relative_path, lineno, "header", text)
                    )
                    continue

                raise SnapshotBuildError(
                    f"Unsupported non-entry source directive at {relative_path}:{lineno}"
                )
        finally:
            self._active_stack.pop()
