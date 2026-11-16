"""Bounded, descriptor-relative capture of an untrusted Beancount workspace.

Only captured bytes reach Beancount's recursive loader. Python plugins remain
trusted server code; this is a source-filesystem boundary, not a plugin sandbox.

The source-only copy supports .bean/.beancount files and an explicitly configured
root with another suffix. Auxiliary document discovery and workspace-local Python
imports are rejected, not silently dropped. No files are created in the ledger.
"""

from __future__ import annotations

import errno
import fnmatch
import os
import stat
from collections import Counter
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from tempfile import TemporaryDirectory
from types import MappingProxyType

from beancount.core import data
from beancount.parser import parser

# Capture, reads, overrides and include expansion all share these hard limits.
# Separate scan/glob limits also bound work on directories containing no sources.
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_WORKSPACE_FILES = 4096
MAX_WORKSPACE_BYTES = 64 * 1024 * 1024
MAX_INCLUDE_VISITS = 4096
MAX_INCLUDE_DEPTH = 64
MAX_EXPANDED_BYTES = 64 * 1024 * 1024
MAX_SCAN_ENTRIES = 32768
MAX_SCAN_DEPTH = 64
MAX_GLOB_STEPS = 262144
SOURCE_SUFFIXES = frozenset({".bean", ".beancount"})


class LedgerWorkspaceError(ValueError):
    """Source files cannot safely be captured or staged for Beancount."""


_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK


def _absolute(path: Path) -> Path:
    # resolve() would erase the symlinks that the descriptor walk must reject.
    return Path(os.path.abspath(path))


@contextmanager
def _directory(path: Path):
    """Open every component without following symlinks, including ancestors."""
    descriptor = os.open("/", _DIRECTORY_FLAGS)
    try:
        for component in _absolute(path).parts[1:]:
            child = os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def _source_error(exc: OSError) -> LedgerWorkspaceError:
    if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
        return LedgerWorkspaceError("Symlink source include is not exportable")
    return LedgerWorkspaceError(f"Ledger source could not be captured: {exc.strerror}")


def _read_file(directory_fd: int, name: str, remaining: int) -> bytes:
    descriptor = os.open(name, _FILE_FLAGS, dir_fd=directory_fd)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise LedgerWorkspaceError(
                f"Included source path is not a regular file: {name}"
            )
        if before.st_size > MAX_FILE_BYTES:
            raise LedgerWorkspaceError("Ledger per-file byte budget exceeded")
        if before.st_size > remaining:
            raise LedgerWorkspaceError("Ledger workspace byte budget exceeded")
        limit = min(remaining, MAX_FILE_BYTES)
        chunks = []
        size = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, limit - size + 1))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_FILE_BYTES:
                raise LedgerWorkspaceError("Ledger per-file byte budget exceeded")
            if size > remaining:
                raise LedgerWorkspaceError("Ledger workspace byte budget exceeded")
        after = os.fstat(descriptor)
        if size != before.st_size or (
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise LedgerWorkspaceError(f"Ledger source changed during capture: {name}")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _scan(directory_fd: int, root_relative: Path) -> tuple[dict[Path, bytes], dict]:
    files: dict[Path, bytes] = {}
    tree: dict[Path, dict[str, str]] = {}
    size = 0
    scanned = 0

    def read(parent_fd: int, relative: Path) -> None:
        nonlocal size
        if len(files) >= MAX_WORKSPACE_FILES:
            raise LedgerWorkspaceError("Ledger workspace file budget exceeded")
        content = _read_file(parent_fd, relative.name, MAX_WORKSPACE_BYTES - size)
        files[relative] = content
        size += len(content)

    read(directory_fd, root_relative)

    def walk(parent_fd: int, relative: Path, depth: int) -> None:
        nonlocal scanned
        if depth > MAX_SCAN_DEPTH:
            raise LedgerWorkspaceError("Ledger workspace scan depth budget exceeded")
        children: dict[str, str] = {}
        tree[relative] = children
        # Iterate before sorting: scandir must not allocate an unbounded listing.
        with os.scandir(parent_fd) as entries:
            for entry in entries:
                scanned += 1
                if scanned > MAX_SCAN_ENTRIES:
                    raise LedgerWorkspaceError(
                        "Ledger workspace scan entry budget exceeded"
                    )
                child = relative / entry.name
                if entry.name.startswith(".") and child != root_relative:
                    continue
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    raise LedgerWorkspaceError(
                        f"Symlink source include is not exportable: {child}"
                    )
                if stat.S_ISDIR(info.st_mode):
                    children[entry.name] = "directory"
                else:
                    children[entry.name] = (
                        "file" if stat.S_ISREG(info.st_mode) else "special"
                    )
        for name, kind in sorted(children.items()):
            child = relative / name
            if kind == "directory":
                descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
                try:
                    walk(descriptor, child, depth + 1)
                finally:
                    os.close(descriptor)
            elif child != root_relative and child.suffix in SOURCE_SUFFIXES:
                read(parent_fd, child)

    walk(directory_fd, Path(), 0)
    return files, tree


def _pattern_parts(pattern: str) -> tuple[str, ...]:
    requested = Path(pattern)
    if (
        not pattern
        or not requested.parts
        or "\x00" in pattern
        or requested.is_absolute()
        or PureWindowsPath(pattern).drive
        or ".." in requested.parts
        or any(part.startswith(".") for part in requested.parts)
        or pattern.endswith("/")
    ):
        raise LedgerWorkspaceError(f"Unsafe source include path: {pattern}")
    if len(requested.parts) - 1 > MAX_SCAN_DEPTH:
        raise LedgerWorkspaceError("Ledger workspace scan depth budget exceeded")
    return requested.parts


def _source_relative(relative_path: Path) -> Path:
    _pattern_parts(str(relative_path))
    if relative_path.suffix not in SOURCE_SUFFIXES:
        raise LedgerWorkspaceError("Only .bean/.beancount source files are supported")
    return relative_path


def read_workspace_file(root_directory: Path, relative_path: Path) -> bytes:
    """Read one nonhidden source using no-follow descriptors at every level.

    Paths are relative to the configured ledger directory, never resolved first.
    Raises LedgerWorkspaceError on unsafe paths, symlinks or special files;
    FileNotFoundError is preserved so HTTP callers can distinguish a missing file.
    """
    relative_path = _source_relative(Path(relative_path))
    if ".." in root_directory.parts:
        raise LedgerWorkspaceError("Unsafe ledger root path: parent traversal")
    try:
        with _directory(root_directory) as root_fd:
            descriptor = os.dup(root_fd)
            try:
                for part in relative_path.parts[:-1]:
                    child = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
                    os.close(descriptor)
                    descriptor = child
                return _read_file(descriptor, relative_path.name, MAX_WORKSPACE_BYTES)
            finally:
                os.close(descriptor)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise _source_error(exc) from exc


def glob_workspace_files(root_directory: Path, pattern: str) -> dict[Path, bytes]:
    """Return sorted, bounded nonhidden source matches, never following symlinks.

    Like Python glob (and Beancount), ** is recursive. Matching directories and
    symlinks are omitted; an explicitly hidden directory is rejected. All reads
    use the descriptors used for traversal, not paths reopened after validation.
    An unmatched pattern returns an empty dictionary.
    """
    parts = _pattern_parts(pattern)
    if ".." in root_directory.parts:
        raise LedgerWorkspaceError("Unsafe ledger root path: parent traversal")
    files: dict[Path, bytes] = {}
    scanned = steps = size = 0

    def walk(parent_fd: int, relative: Path, offset: int, depth: int) -> None:
        nonlocal scanned, steps, size
        steps += 1
        if steps > MAX_GLOB_STEPS:
            raise LedgerWorkspaceError("Ledger include glob expansion budget exceeded")
        if depth > MAX_SCAN_DEPTH:
            raise LedgerWorkspaceError("Ledger workspace scan depth budget exceeded")
        component = parts[offset]
        if component == "**" and offset + 1 < len(parts):
            walk(parent_fd, relative, offset + 1, depth)
        children = []
        with os.scandir(parent_fd) as entries:
            for entry in entries:
                scanned += 1
                if scanned > MAX_SCAN_ENTRIES:
                    raise LedgerWorkspaceError(
                        "Ledger workspace scan entry budget exceeded"
                    )
                if entry.name.startswith("."):
                    continue
                if component != "**" and not fnmatch.fnmatchcase(entry.name, component):
                    continue
                info = entry.stat(follow_symlinks=False)
                children.append((entry.name, info.st_mode))
        for name, mode in sorted(children):
            child_path = relative / name
            if stat.S_ISDIR(mode) and (component == "**" or offset + 1 < len(parts)):
                descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
                try:
                    walk(
                        descriptor,
                        child_path,
                        offset if component == "**" else offset + 1,
                        depth + 1,
                    )
                finally:
                    os.close(descriptor)
            elif (
                offset + 1 == len(parts)
                and stat.S_ISREG(mode)
                and child_path.suffix in SOURCE_SUFFIXES
                and child_path not in files
            ):
                if len(files) >= MAX_WORKSPACE_FILES:
                    raise LedgerWorkspaceError("Ledger workspace file budget exceeded")
                content = _read_file(parent_fd, name, MAX_WORKSPACE_BYTES - size)
                files[child_path] = content
                size += len(content)

    try:
        with _directory(root_directory) as directory_fd:
            walk(directory_fd, Path(), 0, 0)
    except OSError as exc:
        raise _source_error(exc) from exc
    return dict(sorted(files.items()))


def _validate_graph(
    root_relative: Path,
    files: Mapping[Path, bytes],
    tree: Mapping[Path, Mapping[str, str]],
) -> tuple[dict[Path, int], dict[tuple[Path, str], tuple[Path, ...]]]:
    counts: Counter[Path] = Counter()
    includes: dict[tuple[Path, str], tuple[Path, ...]] = {}
    parsed: dict[Path, tuple[str, ...]] = {}
    active: set[Path] = set()
    visits = 0
    expanded_bytes = 0
    glob_steps = 0

    def expand(current: Path, pattern: str) -> tuple[Path, ...]:
        nonlocal glob_steps
        parts = _pattern_parts(pattern)
        matches = []
        pending = [(current.parent, 0)]

        def spend() -> None:
            nonlocal glob_steps
            glob_steps += 1
            if glob_steps > MAX_GLOB_STEPS:
                raise LedgerWorkspaceError(
                    "Ledger include glob expansion budget exceeded"
                )

        while pending:
            spend()
            directory, offset = pending.pop()
            if offset == len(parts):
                if directory not in files:
                    raise LedgerWorkspaceError(
                        f"Included source path is not a supported source regular file: {pattern}"
                    )
                matches.append(directory)
                if len(matches) > MAX_INCLUDE_VISITS:
                    raise LedgerWorkspaceError("Ledger include visit budget exceeded")
                continue
            component = parts[offset]
            children = tree.get(directory, {})
            if component == "**":
                pending.append((directory, offset + 1))
                for name, kind in sorted(children.items(), reverse=True):
                    spend()
                    if kind == "directory":
                        pending.append((directory / name, offset))
            else:
                for name, kind in sorted(children.items(), reverse=True):
                    spend()
                    if name.startswith(".") and not component.startswith("."):
                        continue
                    if fnmatch.fnmatchcase(name, component) and (
                        offset + 1 == len(parts) or kind == "directory"
                    ):
                        pending.append((directory / name, offset + 1))
        if not matches:
            raise LedgerWorkspaceError(f"Included source file not found: {pattern}")
        return tuple(sorted(matches))

    def visit(relative: Path, depth: int) -> None:
        nonlocal visits, expanded_bytes
        visits += 1
        expanded_bytes += len(files[relative])
        if expanded_bytes > MAX_EXPANDED_BYTES:
            raise LedgerWorkspaceError("Ledger expanded source byte budget exceeded")
        if visits > MAX_INCLUDE_VISITS:
            raise LedgerWorkspaceError("Ledger include visit budget exceeded")
        if depth > MAX_INCLUDE_DEPTH:
            raise LedgerWorkspaceError("Ledger include depth budget exceeded")
        if relative in active:
            raise LedgerWorkspaceError(
                f"Cyclic include in ledger source: {relative.name}"
            )
        counts[relative] += 1
        if relative not in parsed:
            try:
                files[relative].decode("utf-8")
            except UnicodeDecodeError as exc:
                raise LedgerWorkspaceError(
                    f"Source file could not be indexed: {relative.name}: invalid UTF-8"
                ) from exc
            entries, errors, options = parser.parse_string(
                files[relative], report_filename=str(relative)
            )
            if errors:
                raise LedgerWorkspaceError(
                    f"Source file could not be indexed: {relative.name}: "
                    + "; ".join(str(error) for error in errors)
                )
            if options["documents"] or any(
                isinstance(entry, data.Document) for entry in entries
            ):
                raise LedgerWorkspaceError(
                    "Document auxiliaries are not supported by a source-only ledger copy"
                )
            if options["insert_pythonpath"]:
                raise LedgerWorkspaceError(
                    "Workspace-local Python imports are not supported by a source-only ledger copy"
                )
            parsed[relative] = tuple(options["include"])
        active.add(relative)
        try:
            for pattern in parsed[relative]:
                key = (relative, pattern)
                if key not in includes:
                    includes[key] = expand(relative, pattern)
                for child in includes[key]:
                    visit(child, depth + 1)
        finally:
            active.remove(relative)

    visit(root_relative, 0)
    return dict(counts), includes


@dataclass(frozen=True, slots=True)
class LedgerWorkspace:
    """Immutable source capture; public dictionaries are defensive copies.

    ``materialize`` requires an empty, private directory outside the source
    workspace, normally supplied by ``tempfile.TemporaryDirectory``. It never
    executes plugins: callers run the full loader on the returned root path.
    """

    root_relative: Path
    _files: Mapping[Path, bytes]
    _include_counts: Mapping[Path, int]
    _includes: Mapping[tuple[Path, str], tuple[Path, ...]]
    _tree: Mapping[Path, Mapping[str, str]]
    _source_directory: Path

    @property
    def files(self) -> dict[Path, bytes]:
        return dict(self._files)

    @property
    def include_counts(self) -> dict[Path, int]:
        return dict(self._include_counts)

    @classmethod
    def capture(cls, root_path: Path) -> LedgerWorkspace:
        if ".." in root_path.parts:
            raise LedgerWorkspaceError("Unsafe ledger root path: parent traversal")
        root_path = _absolute(root_path)
        if "$" in root_path.name or root_path.suffix in {".gpg", ".asc"}:
            raise LedgerWorkspaceError(
                "Expanded or encrypted ledger root filenames are unsupported"
            )
        try:
            with _directory(root_path.parent) as directory_fd:
                files, tree = _scan(directory_fd, Path(root_path.name))
            counts, includes = _validate_graph(Path(root_path.name), files, tree)
        except OSError as exc:
            raise _source_error(exc) from exc
        return cls(
            Path(root_path.name),
            MappingProxyType(files),
            MappingProxyType(counts),
            MappingProxyType(includes),
            MappingProxyType(
                {path: MappingProxyType(children) for path, children in tree.items()}
            ),
            root_path.parent,
        )

    def _included_paths(self, relative: Path, pattern: str) -> tuple[Path, ...]:
        return self._includes[(relative, pattern)]

    def _candidate_files(
        self, overrides: dict[Path, bytes] | None
    ) -> dict[Path, bytes]:
        files = self.files
        for relative, content in (overrides or {}).items():
            if relative not in files or not isinstance(content, bytes):
                raise LedgerWorkspaceError(
                    "Overrides must replace captured source files with bytes"
                )
            if len(content) > MAX_FILE_BYTES:
                raise LedgerWorkspaceError("Ledger per-file byte budget exceeded")
            files[relative] = content
        if sum(map(len, files.values())) > MAX_WORKSPACE_BYTES:
            raise LedgerWorkspaceError("Ledger workspace byte budget exceeded")
        if overrides:
            _validate_graph(self.root_relative, files, self._tree)
        return files

    def _write_files(self, directory_fd: int, files: Mapping[Path, bytes]) -> None:
        info = os.fstat(directory_fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise LedgerWorkspaceError("Materialization directory must be private")
        with os.scandir(directory_fd) as entries:
            if next(entries, None) is not None:
                raise LedgerWorkspaceError("Materialization directory must be empty")
        for relative, content in sorted(files.items()):
            descriptor = os.dup(directory_fd)
            try:
                for part in relative.parts[:-1]:
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                    child = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
                    os.close(descriptor)
                    descriptor = child
                output_fd = os.open(
                    relative.name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o600,
                    dir_fd=descriptor,
                )
                with os.fdopen(output_fd, "wb") as output:
                    output.write(content)
            finally:
                os.close(descriptor)

    def materialize(
        self, directory: Path, overrides: dict[Path, bytes] | None = None
    ) -> Path:
        """Stage in an empty private directory outside the ledger; prefer private_copy.

        Overrides replace captured relative paths only. Their decoded include
        graph is revalidated before any files are written. The caller owns this
        destination's lifetime and must keep its ancestors stable during loading.
        """
        directory = _absolute(directory)
        if directory.is_relative_to(self._source_directory):
            raise LedgerWorkspaceError(
                "Materialization must be outside the ledger workspace"
            )
        files = self._candidate_files(overrides)
        try:
            with _directory(directory.parent) as parent_fd:
                try:
                    os.mkdir(directory.name, mode=0o700, dir_fd=parent_fd)
                except FileExistsError:
                    pass
            with _directory(directory) as directory_fd:
                self._write_files(directory_fd, files)
        except OSError as exc:
            raise LedgerWorkspaceError(
                f"Ledger workspace could not be materialized: {exc.strerror}"
            ) from exc
        return directory / self.root_relative

    @contextmanager
    def private_copy(
        self, overrides: dict[Path, bytes] | None = None
    ) -> Iterator[Path]:
        """Stage this capture in private temporary storage outside the ledger.

        Keeps bytes stable across repeated candidate validations. Overrides use
        workspace-relative paths and cannot add files. Cleanup runs on failure.
        """
        files = self._candidate_files(overrides)
        # gettempdir() probes candidates by writing files before returning. Do not
        # let an unsafe TMPDIR cause even a probe write into the ledger workspace.
        configured_temp = Path(
            os.environ.get("TMPDIR")
            or os.environ.get("TEMP")
            or os.environ.get("TMP")
            or "/tmp"
        )
        if ".." in configured_temp.parts:
            raise LedgerWorkspaceError(
                "Unsafe temporary storage path: parent traversal"
            )
        temporary_root = _absolute(configured_temp)
        if temporary_root.is_relative_to(self._source_directory):
            raise LedgerWorkspaceError(
                "Snapshot temporary storage must be outside the ledger workspace"
            )
        try:
            # Pin TMPDIR before creation; do not follow a swapped directory or
            # symlink between checking its location and creating the private copy.
            with (
                _directory(temporary_root) as temporary_fd,
                TemporaryDirectory(
                    prefix="cashier-snapshot-", dir=f"/proc/self/fd/{temporary_fd}"
                ) as directory,
            ):
                descriptor = os.open(
                    Path(directory).name, _DIRECTORY_FLAGS, dir_fd=temporary_fd
                )
                try:
                    self._write_files(descriptor, files)
                    # Anchor the yielded root to the private directory itself,
                    # even if a configured temporary-directory owner renames it.
                    yield Path(f"/proc/self/fd/{descriptor}") / self.root_relative
                finally:
                    os.close(descriptor)
        except OSError as exc:
            raise _source_error(exc) from exc


@contextmanager
def private_ledger_copy(
    root_path: Path, overrides: dict[Path, bytes] | None = None
) -> Iterator[Path]:
    """Capture a safe source tree and yield its private root for the full loader.

    This does not execute plugins. Callers must run the configured full pipeline
    on the yielded path, not root_path. Overrides replace captured relative paths
    only, and the candidate's decoded include closure is validated before yield.
    """
    with LedgerWorkspace.capture(root_path).private_copy(overrides) as private_root:
        yield private_root
