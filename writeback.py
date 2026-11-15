"""Stage 2's single-file, append-only transaction write channel.

This synchronous function must run in a worker, never on the event loop: flock
covers capture, read, deduplication, validation and durable append. POSIX flock,
O_NOFOLLOW and directory-relative opens are required; Windows fails closed.

Each new batch has reserved ``; cashier-writeback-v1`` checksum comments. Its
begin marker is fsynced before transaction bytes. A killed process can leave an
unfinished batch, including a syntactically valid transaction prefix. On restart
we refuse further writes rather than guessing which user bytes to discard. An
operator must stop all writeback workers, preserve the damaged bytes for review,
restore a verified known-good manual file, and validate the full ledger before
restarting. Never discard an unverified suffix merely to clear a recovery error.
Catchable append failures roll back to the original byte length only if the
current bytes are a verified prefix of this attempt, then fsync under the same
lock. This detects incomplete batches; it does not promise crash atomicity. No
lockfile, journal, rename or temp file is used in the ledger.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import stat
import unicodedata
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO

from beancount import loader
from beancount.core import data, display_context
from beancount.parser import grammar, parser
from beancount.parser.printer import EntryPrinter
from loguru import logger

if TYPE_CHECKING:
    from cashier_snapshot.workspace import LedgerWorkspace

try:
    import fcntl
except ImportError:  # Windows must not silently fall back to a process-local lock.
    fcntl = None

MAX_TRANSACTIONS = 100
MAX_TRANSACTION_BYTES = 64 * 1024
MAX_POSTINGS = 256
MAX_METADATA = 64
MAX_METADATA_KEY_LENGTH = 64
MAX_METADATA_STRING_BYTES = 1024
MAX_CASHIER_ID_LENGTH = 128

_MARKER = b"; cashier-writeback-v1 "
# If even recovery fails, no later request in this worker may report success.
_recovery_failures: set[tuple[int, int]] = set()


class _TransactionBuilder(grammar.Builder):
    """Reject directives which normally vanish into parser options/state."""

    def __init__(self):
        super().__init__()
        self.metadata_count = 0

    def key_value(self, filename, lineno, key, value):
        # Count source fields, including user-supplied filename/lineno keys,
        # before Builder merges them with its own source-location metadata.
        self.metadata_count += 1
        if self.metadata_count == MAX_METADATA + 1:
            self.errors.append(grammar.ParserError(
                data.new_metadata(filename, lineno),
                f"Transaction exceeds limit: {MAX_METADATA} metadata entries",
                None,
            ))
        if (
            len(key) > MAX_METADATA_KEY_LENGTH
            or key in {"filename", "lineno"}
            or key.startswith("__")
        ):
            self.errors.append(grammar.ParserError(
                data.new_metadata(filename, lineno),
                "Metadata keys must be bounded, non-reserved names",
                None,
            ))
        if isinstance(value, str) and (
            len(value.encode("utf-8")) > MAX_METADATA_STRING_BYTES
            or (value and not value.isprintable())
            or unicodedata.normalize("NFC", value) != value
        ):
            self.errors.append(grammar.ParserError(
                data.new_metadata(filename, lineno),
                f"Metadata strings must be printable NFC text of at most {MAX_METADATA_STRING_BYTES} bytes",
                None,
            ))
        return super().key_value(filename, lineno, key, value)

    def _reject_control(self, filename, lineno):
        self.errors.append(grammar.ParserError(
            data.new_metadata(filename, lineno),
            "Parser control directives are not allowed in transactions",
            None,
        ))

    def option(self, filename, lineno, key, value):
        self._reject_control(filename, lineno)

    def include(self, filename, lineno, include_filename):
        self._reject_control(filename, lineno)

    def plugin(self, filename, lineno, plugin_name, plugin_config):
        self._reject_control(filename, lineno)

    def pushtag(self, filename, lineno, tag):
        self._reject_control(filename, lineno)

    def poptag(self, filename, lineno, tag):
        self._reject_control(filename, lineno)

    def pushmeta(self, filename, lineno, key_value):
        self._reject_control(filename, lineno)

    def popmeta(self, filename, lineno, key):
        self._reject_control(filename, lineno)


def _parse_transaction(text: str):
    # parse_string creates its own Builder; passing builder= through **kw is not
    # supported by the C parser. Use that same parser with our restricted builder.
    builder = _TransactionBuilder()
    c_parser = import_module("beancount.parser._parser")
    c_parser.Parser(builder).parse(io.BytesIO(text.encode("utf-8")), filename="<cashier>")
    return builder.finalize()


def _get_manual_transactions_file() -> str | None:
    return os.environ.get("BEANCOUNT_MANUAL_TRANSACTIONS_FILE")


def _get_bean_file() -> str | None:
    return os.environ.get("BEANCOUNT_FILE")


def _absolute_path(filepath: str) -> Path:
    path = Path(filepath).absolute()
    if ".." in path.parts:
        raise ValueError("Ledger paths must not contain parent traversal")
    return path


def _open_manual_fd(filepath: str, *, writable: bool = True) -> int:
    """Walk every directory without following symlinks; never create the file."""
    if (
        fcntl is None
        or not hasattr(os, "O_NOFOLLOW")
        or not hasattr(os, "O_DIRECTORY")
        or os.open not in os.supports_dir_fd
    ):
        raise RuntimeError("Writeback requires POSIX flock and safe O_NOFOLLOW opens; Windows is unsupported")
    path = _absolute_path(filepath)
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    directory_fd = os.open(path.anchor, directory_flags)
    try:
        for component in path.parts[1:-1]:
            child_fd = os.open(component, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = child_fd
        flags = os.O_RDWR | os.O_APPEND if writable else os.O_RDONLY
        fd = os.open(
            path.name, flags | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
            dir_fd=directory_fd,
        )
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise RuntimeError("Manual transactions file must be an existing, single-link regular file")
        except BaseException:
            os.close(fd)
            raise
        return fd
    finally:
        os.close(directory_fd)


def _file_identity(fd: int) -> tuple[int, int]:
    info = os.fstat(fd)
    return info.st_dev, info.st_ino


def _assert_manual_identity(fd: int, filepath: str) -> None:
    check_fd = _open_manual_fd(filepath, writable=False)
    try:
        if _file_identity(fd) != _file_identity(check_fd):
            raise RuntimeError("Manual transactions file identity changed; refusing to write")
    finally:
        os.close(check_fd)


@contextmanager
def _locked_manual_file(filepath: str):
    fd = _open_manual_fd(filepath)
    try:
        assert fcntl is not None
        fcntl.flock(fd, fcntl.LOCK_EX)
        _assert_manual_identity(fd, filepath)
        if _file_identity(fd) in _recovery_failures:
            raise RuntimeError("Manual file recovery previously failed; operator recovery is required")
        yield fd
    finally:
        # Closing the pinned descriptor releases flock even on an exception.
        os.close(fd)


def _read_manual_transactions_bytes(fd: int) -> bytes:
    from cashier_snapshot.workspace import MAX_FILE_BYTES

    if os.fstat(fd).st_size > MAX_FILE_BYTES:
        raise RuntimeError("Manual transactions file exceeds the source byte budget")
    os.lseek(fd, 0, os.SEEK_SET)
    chunks = []
    size = 0
    while chunk := os.read(fd, min(1024 * 1024, MAX_FILE_BYTES - size + 1)):
        chunks.append(chunk)
        size += len(chunk)
        if size > MAX_FILE_BYTES:
            raise RuntimeError("Manual transactions file exceeds the source byte budget")
    return b"".join(chunks)


def _check_writeback_integrity(content: bytes) -> None:
    digest = None
    pending_hash = None
    for line in content.splitlines(keepends=True):
        if not line.endswith(b"\n") and _MARKER.startswith(line):
            raise RuntimeError("Incomplete writeback marker; operator recovery is required")
        if line.startswith(_MARKER):
            parts = line.rstrip(b"\n").split(b" ")
            if (
                not line.endswith(b"\n") or len(parts) != 4
                or parts[2] not in (b"begin", b"end")
                or re.fullmatch(rb"[0-9a-f]{64}", parts[3]) is None
            ):
                raise RuntimeError("Incomplete writeback marker; operator recovery is required")
            if parts[2] == b"begin":
                if digest is not None:
                    raise RuntimeError("Incomplete writeback batch; operator recovery is required")
                digest, pending_hash = parts[3], hashlib.sha256()
            else:
                if pending_hash is None or digest != parts[3] or pending_hash.hexdigest().encode() != digest:
                    raise RuntimeError("Corrupt writeback batch; operator recovery is required")
                digest, pending_hash = None, None
        elif pending_hash is not None:
            pending_hash.update(line)
    if pending_hash is not None:
        raise RuntimeError("Incomplete writeback batch; operator recovery is required")


def _capture_workspace(manual_file: str, previous: bytes) -> tuple[LedgerWorkspace, Path]:
    # Check platform support while opening/locking, before importing POSIX-only
    # workspace capture code. Unsupported systems return a configuration error.
    from cashier_snapshot.workspace import LedgerWorkspace, LedgerWorkspaceError

    bean_file = _get_bean_file()
    if not bean_file:
        raise ValueError("BEANCOUNT_FILE not configured")
    root = _absolute_path(bean_file)
    try:
        manual_relative = _absolute_path(manual_file).relative_to(root.parent)
    except ValueError as exc:
        raise LedgerWorkspaceError("Manual transactions file must be inside the ledger root") from exc
    workspace = LedgerWorkspace.capture(root)
    if manual_relative == workspace.root_relative:
        raise LedgerWorkspaceError("Manual transactions file must be an included file, not the ledger root")
    if workspace.include_counts.get(manual_relative, 0) != 1:
        raise LedgerWorkspaceError("Manual transactions file must be included exactly once in the full ledger")
    if workspace.files.get(manual_relative) != previous:
        raise LedgerWorkspaceError("Captured manual transactions file differs from the locked file")
    return workspace, manual_relative


@dataclass
class _ValidationWorkspace:
    workspace: LedgerWorkspace
    root: Path
    manual: BinaryIO
    content: bytes


@contextmanager
def _private_validation_root(workspace: LedgerWorkspace, manual_relative: Path):
    # The workspace API pins its temporary ancestors and yields a /proc fd root.
    # Open the staged manual file once, walking its relative parents no-follow.
    # Candidate updates must never reopen a TMPDIR-based pathname.
    resources = ExitStack()
    try:
        root = resources.enter_context(workspace.private_copy())
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
        directory_fd = os.open(root.parent, directory_flags)
        try:
            for component in manual_relative.parts[:-1]:
                child_fd = os.open(component, directory_flags | os.O_NOFOLLOW, dir_fd=directory_fd)
                os.close(directory_fd)
                directory_fd = child_fd
            fd = os.open(
                manual_relative.name, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                dir_fd=directory_fd,
            )
        finally:
            os.close(directory_fd)
        manual = resources.enter_context(os.fdopen(fd, "r+b", buffering=0))
        info = os.fstat(manual.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RuntimeError("Private manual copy must be a single-link regular file")
        yield _ValidationWorkspace(workspace, root, manual, workspace.files[manual_relative])
    finally:
        # Cleanup without forwarding commit errors through private_copy(), which
        # translates OSError into source-capture errors across its yield.
        resources.close()


def _load_workspace(validation: _ValidationWorkspace, manual_relative: Path):
    # Revalidate the complete decoded include closure and every source budget
    # before staging or loading, including for baseline and final validation.
    validation.workspace._candidate_files({manual_relative: validation.content})
    validation.manual.seek(0)
    remaining = memoryview(validation.content)
    while remaining:
        written = validation.manual.write(remaining)
        if not written:
            raise OSError("Private candidate write made no forward progress")
        remaining = remaining[written:]
    validation.manual.truncate()
    validation.manual.flush()
    # Reusing a private tree must never reuse a cached result for an older
    # candidate. This runs the full parser/booking/trusted-plugin pipeline.
    entries, errors, options = loader._uncached_load_file(str(validation.root), None, None, None)
    expected_manual = str(validation.root.parent / manual_relative)
    if options.get("include", []).count(expected_manual) != 1:
        raise RuntimeError("Full-ledger loader did not load the staged manual file exactly once")
    return entries, [str(error) for error in errors]


def _load_full_ledger(validation: _ValidationWorkspace, manual_relative: Path):
    entries, errors = _load_workspace(validation, manual_relative)
    if errors:
        raise RuntimeError("Authoritative full ledger has errors; refusing to write: " + "; ".join(errors))
    return entries


def _extract_cashier_id(entry: data.Transaction) -> str | None:
    """IDs are canonical, case-sensitive ASCII keys, 1..128 characters."""
    if not isinstance(entry, data.Transaction):
        return None
    cid = (entry.meta or {}).get("cashier_id")
    if isinstance(cid, str) and re.fullmatch(
        rf"[A-Za-z0-9][A-Za-z0-9._:-]{{0,{MAX_CASHIER_ID_LENGTH - 1}}}", cid,
    ):
        return cid
    return None


def _validate_accounts(entry: data.Transaction, existing_entries: list) -> list:
    open_accounts = {e.account for e in existing_entries if isinstance(e, data.Open)}
    return [posting.account for posting in entry.postings if posting.account not in open_accounts]


def _transaction_semantics(entry: data.Transaction) -> data.Transaction:
    """Compare parsed values, ignoring only parser-supplied source locations."""
    def metadata(meta):
        return {key: value for key, value in (meta or {}).items() if key not in {"filename", "lineno"}}

    return entry._replace(
        meta=metadata(entry.meta),
        postings=[posting._replace(meta=metadata(posting.meta)) for posting in entry.postings],
    )


def _normalize_entry(entry: data.Transaction) -> str:
    """Render without rounding, and require exactly one unchanged transaction.

    Printer quoting is not a security boundary. In particular, lot labels must
    survive restricted parsing and semantic comparison before any loader sees
    the rendered bytes.
    """
    ordered = entry._replace(
        meta=dict(sorted((entry.meta or {}).items())),
        postings=[posting._replace(meta=dict(sorted((posting.meta or {}).items()))) for posting in entry.postings],
    )
    try:
        rendered = EntryPrinter(dcontext=display_context.DisplayContext())(ordered)
        entries, errors, _ = _parse_transaction(rendered)
        if (
            errors or len(entries) != 1 or not isinstance(entries[0], data.Transaction)
            or _transaction_semantics(entries[0]) != _transaction_semantics(entry)
        ):
            raise ValueError("Restricted semantic roundtrip failed")
        return rendered
    except Exception as exc:
        raise ValueError("Transaction serialization must preserve exactly one unchanged transaction") from exc


def _append_entries_to_source(existing_content: str, valid_new_entries: list) -> str:
    """Preserve existing bytes and delimit the new batch for restart detection."""
    blocks = [_normalize_entry(entry) for entry in valid_new_entries if isinstance(entry, data.Transaction)]
    if not blocks:
        return existing_content
    body = "\n\n".join(blocks) + "\n"
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    prefix = existing_content
    if prefix and not prefix.endswith("\n"):
        prefix += "\n"
    if prefix.strip():
        prefix += "\n"
    return (
        prefix + f"; cashier-writeback-v1 begin {digest}\n" + body
        + f"; cashier-writeback-v1 end {digest}\n"
    )


def _write_all(fd: int, content: bytes) -> None:
    remaining = memoryview(content)
    while remaining:
        written = os.write(fd, remaining)
        if written <= 0:
            raise OSError("Append made no forward progress")
        remaining = remaining[written:]


def _write_file_crash_conscious(fd: int, previous_content: bytes, candidate_content: bytes) -> None:
    """Append on the locked inode, with durable intent and catchable rollback."""
    from cashier_snapshot.workspace import MAX_FILE_BYTES

    if len(candidate_content) > MAX_FILE_BYTES:
        raise RuntimeError("Candidate manual file exceeds the per-file source byte budget")
    if not candidate_content.startswith(previous_content):
        raise RuntimeError("Candidate write is not append-only; refusing to modify manual file")
    if _read_manual_transactions_bytes(fd) != previous_content:
        raise RuntimeError("Manual transactions file changed during validation; refusing to append")
    suffix = candidate_content[len(previous_content):]
    if not suffix:
        return
    begin = suffix.find(_MARKER + b"begin ")
    if begin < 0:
        raise RuntimeError("Candidate is missing its writeback recovery marker")
    boundary = suffix.index(b"\n", begin) + 1
    try:
        _write_all(fd, suffix[:boundary])
        os.fsync(fd)
        _write_all(fd, suffix[boundary:])
        os.fsync(fd)
        if _read_manual_transactions_bytes(fd) != candidate_content:
            raise RuntimeError("Manual transactions file changed during append; refusing to synchronize")
    except BaseException:
        try:
            current = _read_manual_transactions_bytes(fd)
            if not current.startswith(previous_content) or not candidate_content.startswith(current):
                raise RuntimeError("Unverified external bytes prevent safe append rollback")
            os.ftruncate(fd, len(previous_content))
            os.fsync(fd)
        except BaseException as recovery_error:
            _recovery_failures.add(_file_identity(fd))
            raise RuntimeError(
                "Manual file append and recovery failed; stop writeback and perform operator recovery"
            ) from recovery_error
        raise


def _validate_candidate_as_full_ledger(content: str, validation: _ValidationWorkspace, manual_relative: Path) -> list:
    """Revalidate the full candidate graph before a descriptor-pinned load."""
    try:
        candidate = content.encode("utf-8")
        _check_writeback_integrity(candidate)
        validation.content = candidate
        _, errors = _load_workspace(validation, manual_relative)
        return errors
    except Exception as exc:
        logger.exception("Full-ledger validation error")
        return [f"Validation error: {exc}"]


def _validation_error_texts_for_entry(
    base_content: str, entry: data.Transaction, validation: _ValidationWorkspace, manual_relative: Path,
) -> list:
    return _validate_candidate_as_full_ledger(
        _append_entries_to_source(base_content, [entry]), validation, manual_relative,
    )


def _unique_ids(ids: list[str]) -> list[str]:
    return list(dict.fromkeys(cid for cid in ids if cid))


def validate_and_commit(transactions_texts: list[str]) -> tuple[dict, bool]:
    """Blocking worker operation; return synchronized IDs only after durable commit."""
    if len(transactions_texts) > MAX_TRANSACTIONS:
        raise ValueError(f"At most {MAX_TRANSACTIONS} transactions are allowed per request")
    manual_file = _get_manual_transactions_file()
    if not manual_file:
        raise RuntimeError("BEANCOUNT_MANUAL_TRANSACTIONS_FILE not configured")

    with _locked_manual_file(manual_file) as fd, ExitStack() as validation_stack:
        previous_bytes = _read_manual_transactions_bytes(fd)
        _check_writeback_integrity(previous_bytes)
        existing_content = previous_bytes.decode("utf-8")
        existing_manual_entries, existing_errors, _ = parser.parse_string(existing_content)
        if existing_errors:
            raise RuntimeError(
                "Existing manual transactions file has parse errors; refusing to write: "
                + "; ".join(str(error) for error in existing_errors)
            )
        existing_by_id = {}
        for entry in existing_manual_entries:
            if isinstance(entry, data.Transaction):
                cid = _extract_cashier_id(entry)
                if "cashier_id" in (entry.meta or {}) and cid is None:
                    raise RuntimeError("Existing manual file has invalid cashier_id metadata; refusing to write")
                if cid:
                    if cid in existing_by_id:
                        raise RuntimeError(
                            "Existing manual transactions file contains duplicate "
                            f"cashier_id {cid}; refusing to write"
                        )
                    existing_by_id[cid] = entry

        workspace, manual_relative = _capture_workspace(manual_file, previous_bytes)
        validation = validation_stack.enter_context(_private_validation_root(workspace, manual_relative))
        full_entries = _load_full_ledger(validation, manual_relative)
        synchronized = []
        rejected = []
        valid_new = []
        seen_ids_in_request = {}
        accepted_new_ids = []

        for text in transactions_texts:
            try:
                too_large = len(text.encode("utf-8")) > MAX_TRANSACTION_BYTES
            except UnicodeEncodeError:
                rejected.append({"cashier_id": None, "reason": "Transaction text must be valid UTF-8"})
                continue
            if too_large:
                rejected.append({"cashier_id": None, "reason": f"Transaction text exceeds {MAX_TRANSACTION_BYTES} bytes"})
                continue
            parsed_entries, parse_errors, _ = _parse_transaction(text)
            if parse_errors or not parsed_entries:
                rejected.append({
                    "cashier_id": None,
                    "reason": f"Parse error: {parse_errors[0]}" if parse_errors else "Empty input",
                })
                continue
            if len(parsed_entries) != 1:
                rejected.append({
                    "cashier_id": None,
                    "reason": f"Expected exactly one transaction, got {len(parsed_entries)} directive(s)",
                })
                continue
            entry = parsed_entries[0]
            if not isinstance(entry, data.Transaction):
                rejected.append({
                    "cashier_id": None,
                    "reason": f"Expected a transaction directive, got {type(entry).__name__}",
                })
                continue
            if entry.flag != "*":
                rejected.append({
                    "cashier_id": None,
                    "reason": f"Transaction with flag '{entry.flag}' rejected; only completed '*' transactions are accepted",
                })
                continue
            cashier_id = _extract_cashier_id(entry)
            if not cashier_id:
                rejected.append({"cashier_id": None, "reason": "Missing or invalid cashier_id metadata"})
                continue
            if len(entry.postings) > MAX_POSTINGS:
                rejected.append({
                    "cashier_id": cashier_id,
                    "reason": f"Transaction exceeds limit: {MAX_POSTINGS} postings",
                })
                continue
            try:
                normalized_entry = _normalize_entry(entry)
            except ValueError as exc:
                rejected.append({"cashier_id": cashier_id, "reason": str(exc)})
                continue
            unknown = _validate_accounts(entry, full_entries)
            if unknown:
                rejected.append({"cashier_id": cashier_id, "reason": f"Unknown account(s): {', '.join(unknown)}"})
                continue
            if cashier_id in existing_by_id:
                if normalized_entry == _normalize_entry(existing_by_id[cashier_id]):
                    synchronized.append(cashier_id)
                else:
                    rejected.append({
                        "cashier_id": cashier_id,
                        "reason": "Conflict: cashier_id already exists with different transaction content",
                    })
                continue
            if cashier_id in seen_ids_in_request:
                if _normalize_entry(seen_ids_in_request[cashier_id]) != normalized_entry:
                    rejected.append({
                        "cashier_id": cashier_id,
                        "reason": "Conflict: duplicate cashier_id in request with different content",
                    })
                # Identical pending duplicates are NOT synchronized until the append.
                continue
            validation_base = _append_entries_to_source(existing_content, valid_new)
            validation_errors = _validation_error_texts_for_entry(validation_base, entry, validation, manual_relative)
            if validation_errors:
                rejected.append({
                    "cashier_id": cashier_id,
                    "reason": f"Transaction validation failed: {'; '.join(validation_errors)}",
                })
                continue
            seen_ids_in_request[cashier_id] = entry
            valid_new.append(entry)
            accepted_new_ids.append(cashier_id)

        _assert_manual_identity(fd, manual_file)
        if _read_manual_transactions_bytes(fd) != previous_bytes:
            raise RuntimeError("Manual transactions file changed during validation; refusing to synchronize")
        if not valid_new:
            return {"synchronized": _unique_ids(synchronized), "rejected": rejected}, False
        candidate = _append_entries_to_source(existing_content, valid_new)
        validation_errors = _validate_candidate_as_full_ledger(candidate, validation, manual_relative)
        if validation_errors:
            rejected.extend({
                "cashier_id": cid,
                "reason": f"Candidate file validation failed: {'; '.join(validation_errors)}",
            } for cid in accepted_new_ids)
            return {"synchronized": _unique_ids(synchronized), "rejected": rejected}, False

        _assert_manual_identity(fd, manual_file)
        logger.info(f"Writing {len(valid_new)} new transaction(s) to {manual_file}")
        _write_file_crash_conscious(fd, previous_bytes, candidate.encode("utf-8"))
        _assert_manual_identity(fd, manual_file)
        synchronized.extend(accepted_new_ids)
        return {"synchronized": _unique_ids(synchronized), "rejected": rejected}, True
