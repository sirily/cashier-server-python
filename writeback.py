"""Manual transaction writeback for Stage 2.

Accepts validated Cashier-created Beancount transaction directives and appends
them to the configured BEANCOUNT_MANUAL_TRANSACTIONS_FILE.
"""

import json
import os
import shutil
import threading
import tempfile
from pathlib import Path
from typing import Optional

from beancount.core import data
from beancount.parser import parser
from beancount.parser.printer import EntryPrinter
from beancount import loader
from loguru import logger

_lock = threading.Lock()


def _get_manual_transactions_file() -> Optional[str]:
    return os.environ.get("BEANCOUNT_MANUAL_TRANSACTIONS_FILE")


def _get_bean_file() -> Optional[str]:
    return os.environ.get("BEANCOUNT_FILE")


def _load_full_ledger():
    """Load the full configured ledger (main.bean + all includes)."""
    bean_file = _get_bean_file()
    if not bean_file:
        raise ValueError("BEANCOUNT_FILE not configured")
    entries, errors, options_map = loader.load_file(bean_file)
    return entries, errors, options_map


def _extract_cashier_id(entry: data.Transaction) -> Optional[str]:
    """Extract cashier_id from transaction metadata."""
    if not isinstance(entry, data.Transaction):
        return None
    meta = entry.meta or {}
    cid = meta.get("cashier_id")
    if isinstance(cid, str) and cid.strip():
        return cid.strip()
    return None


def _validate_accounts(entry: data.Transaction, existing_entries: list) -> list:
    """Return list of account names in the transaction that don't exist in the ledger."""
    open_accounts = set()
    for e in existing_entries:
        if isinstance(e, data.Open):
            open_accounts.add(e.account)
        if isinstance(e, data.Transaction):
            for p in e.postings:
                open_accounts.add(p.account)
        if isinstance(e, data.Balance):
            open_accounts.add(e.account)
    return [
        posting.account
        for posting in entry.postings
        if posting.account not in open_accounts
    ]


def _normalize_entry(entry: data.Transaction, options_map: dict) -> str:
    """Normalize a transaction to stable text using Beancount's printer."""
    eprinter = EntryPrinter(
        dcontext=options_map.get("dcontext"),
    )
    return eprinter(entry)


def _normalize_entry_without_cashier_id(entry: data.Transaction, options_map: dict) -> str:
    """Normalize user-visible transaction content independently of its sync ID."""
    meta = {
        key: value
        for key, value in (entry.meta or {}).items()
        if key not in {"cashier_id", "filename", "lineno"}
    }
    return _normalize_entry(entry._replace(meta=meta), options_map)


def _insert_cashier_ids_into_source(
    source: str,
    matches: list[tuple[data.Transaction, str]],
) -> str:
    """Insert IDs after matched transaction headers without reprinting source."""
    lines = source.splitlines(keepends=True)
    for entry, cashier_id in sorted(
        matches,
        key=lambda match: int((match[0].meta or {}).get("lineno", 0)),
        reverse=True,
    ):
        lineno = (entry.meta or {}).get("lineno")
        if not isinstance(lineno, int) or lineno < 1 or lineno > len(lines):
            raise RuntimeError("Existing transaction has no usable source location")
        header_index = lineno - 1
        if not lines[header_index].lstrip().startswith(("20", "19")):
            raise RuntimeError("Existing transaction source location does not point to a header")
        indent = "  "
        if header_index + 1 < len(lines):
            next_line = lines[header_index + 1]
            whitespace = next_line[: len(next_line) - len(next_line.lstrip())]
            if whitespace:
                indent = whitespace
        lines.insert(header_index + 1, f'{indent}cashier_id: {json.dumps(cashier_id)}\n')
    return "".join(lines)


def _read_manual_transactions_text(filepath: str) -> str:
    """Read manual_transactions.bean as source text."""
    path = Path(filepath)
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _append_entries_to_source(
    existing_content: str,
    valid_new_entries: list,
    options_map: dict,
) -> str:
    """Append normalized new transactions without reprinting existing source."""
    if not valid_new_entries:
        return existing_content

    eprinter = EntryPrinter(
        dcontext=options_map.get("dcontext"),
    )
    new_blocks = [
        text
        for entry in valid_new_entries
        if isinstance(entry, data.Transaction)
        for text in [eprinter(entry)]
        if text
    ]
    if not new_blocks:
        return existing_content

    prefix = existing_content
    if prefix and not prefix.endswith("\n"):
        prefix += "\n"
    if prefix.strip():
        prefix += "\n"
    return prefix + "\n\n".join(new_blocks) + "\n"


def _write_file_crash_conscious(
    filepath: str,
    previous_content: str,
    candidate_content: str,
) -> None:
    """Crash-conscious single-file write.

    Append when possible. Linking a unique identifier-less desktop record
    requires a source-preserving in-place rewrite of this one mounted file.
    """
    if candidate_content == previous_content:
        return

    if candidate_content.startswith(previous_content):
        with open(filepath, "a", encoding="utf-8") as f:
            f.write(candidate_content[len(previous_content):])
            f.flush()
            os.fsync(f.fileno())
        return

    with open(filepath, "r+", encoding="utf-8") as f:
        f.seek(0)
        f.write(candidate_content)
        f.truncate()
        f.flush()
        os.fsync(f.fileno())


def _validate_candidate_as_full_ledger(content: str, options_map: dict) -> list:
    """Validate candidate manual_transactions.bean as part of the full ledger.

    Creates a private temp directory outside the ledger workspace, copies the
    configured ledger root tree there, replaces manual_transactions.bean with
    the candidate content, and loads the copied main.bean through the full
    Beancount pipeline (loader.load_file).

    Returns a list of error messages (empty means valid).
    """
    if not content.strip():
        return []

    bean_file = _get_bean_file()
    manual_file = _get_manual_transactions_file()
    if not bean_file or not manual_file:
        return ["BEANCOUNT_FILE or BEANCOUNT_MANUAL_TRANSACTIONS_FILE not configured"]

    ledger_root = Path(bean_file).resolve().parent
    manual_path = Path(manual_file).resolve()

    try:
        manual_rel = manual_path.relative_to(ledger_root)
    except ValueError:
        return [
            "manual_transactions.bean is outside the ledger root; "
            "candidate validation requires the manual file to be inside the ledger root"
        ]

    tmp_dir = Path(tempfile.mkdtemp(prefix="cashier_validation_"))
    skip_names = {'.git', '__pycache__', '.pytest_cache', 'node_modules',
                  '.venv', '.vscode', '.mypy_cache', '.ruff_cache'}
    try:
        for item in ledger_root.iterdir():
            if item.name in skip_names:
                continue
            dst = tmp_dir / item.name
            if item.is_dir():
                shutil.copytree(item, dst, symlinks=False,
                                ignore=shutil.ignore_patterns(*skip_names))
            else:
                shutil.copy2(item, dst)

        tmp_manual = tmp_dir / manual_rel
        tmp_manual.parent.mkdir(parents=True, exist_ok=True)
        tmp_manual.write_text(content, encoding="utf-8")

        tmp_main = tmp_dir / Path(bean_file).name
        _, errors, _ = loader.load_file(str(tmp_main))

        return [str(e) for e in errors]
    except Exception as exc:
        logger.warning("Full-ledger validation error: {}", exc)
        return [f"Validation error: {exc}"]
    finally:
        shutil.rmtree(str(tmp_dir), ignore_errors=True)


def _validation_error_texts_for_entry(
    base_content: str,
    entry: data.Transaction,
    options_map: dict,
) -> list:
    """Validate one candidate entry in the full ledger context."""
    candidate = _append_entries_to_source(base_content, [entry], options_map)
    return _validate_candidate_as_full_ledger(candidate, options_map)


def _unique_ids(ids: list[str]) -> list[str]:
    """Return synchronized ids in first-seen order without duplicates."""
    return list(dict.fromkeys(cid for cid in ids if cid))


def validate_and_commit(transactions_texts: list[str]) -> tuple[dict, bool]:
    """Validate incoming transaction texts and commit valid ones.

    Returns:
        Tuple of (result_dict, wrote_flag).
        ``result_dict`` has keys ``synchronized`` (list of cashier_ids) and
        ``rejected`` (list of dicts with cashier_id and reason).
        ``wrote_flag`` is True if the file was modified.
    """
    manual_file = _get_manual_transactions_file()
    if not manual_file:
        raise RuntimeError("BEANCOUNT_MANUAL_TRANSACTIONS_FILE not configured")

    wrote = False

    with _lock:
        existing_content = _read_manual_transactions_text(manual_file)
        existing_manual_entries, existing_errors, _ = parser.parse_string(existing_content)
        if existing_errors:
            raise RuntimeError(
                "Existing manual transactions file has parse errors; refusing to write: "
                + "; ".join(str(error) for error in existing_errors)
            )

        existing_by_id = {}
        existing_without_id = []
        for entry in existing_manual_entries:
            if isinstance(entry, data.Transaction):
                cid = _extract_cashier_id(entry)
                if cid:
                    if cid in existing_by_id:
                        raise RuntimeError(
                            "Existing manual transactions file contains duplicate "
                            f"cashier_id {cid}; refusing to write"
                        )
                    existing_by_id[cid] = entry
                else:
                    existing_without_id.append(entry)

        full_entries, _, full_options = _load_full_ledger()

        synchronized = []
        rejected = []
        valid_new = []
        source_id_matches = []
        seen_ids_in_request = {}
        accepted_new_ids = []

        for text in transactions_texts:
            parsed_entries, parse_errors, _ = parser.parse_string(text)
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
                type_name = type(entry).__name__
                rejected.append({
                    "cashier_id": None,
                    "reason": f"Expected a transaction directive, got {type_name}",
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
                rejected.append({
                    "cashier_id": None,
                    "reason": "Missing or invalid cashier_id metadata",
                })
                continue

            unknown = _validate_accounts(entry, full_entries)
            if unknown:
                rejected.append({
                    "cashier_id": cashier_id,
                    "reason": f"Unknown account(s): {', '.join(unknown)}",
                })
                continue

            if cashier_id in existing_by_id:
                normalized_new = _normalize_entry(entry, full_options)
                normalized_existing = _normalize_entry(existing_by_id[cashier_id], full_options)
                if normalized_new == normalized_existing:
                    synchronized.append(cashier_id)
                else:
                    rejected.append({
                        "cashier_id": cashier_id,
                        "reason": "Conflict: cashier_id already exists with different transaction content",
                    })
                continue

            if cashier_id in seen_ids_in_request:
                prev_entry = seen_ids_in_request[cashier_id]
                normalized_prev = _normalize_entry(prev_entry, full_options)
                normalized_cur = _normalize_entry(entry, full_options)
                if normalized_prev == normalized_cur:
                    if cashier_id in accepted_new_ids:
                        synchronized.append(cashier_id)
                else:
                    rejected.append({
                        "cashier_id": cashier_id,
                        "reason": "Conflict: duplicate cashier_id in request with different content",
                    })
                continue

            normalized_entry = _normalize_entry_without_cashier_id(entry, full_options)
            matching_desktop_entries = [
                existing
                for existing in existing_without_id
                if existing.date == entry.date
                and all(existing is not matched for matched, _ in source_id_matches)
                and _normalize_entry_without_cashier_id(existing, full_options) == normalized_entry
            ]
            if len(matching_desktop_entries) == 1:
                source_id_matches.append((matching_desktop_entries[0], cashier_id))
                seen_ids_in_request[cashier_id] = entry
                continue
            if len(matching_desktop_entries) > 1:
                rejected.append({
                    "cashier_id": cashier_id,
                    "reason": "Ambiguous existing transaction match; this transaction was not changed",
                })
                continue

            validation_base = _append_entries_to_source(existing_content, valid_new, full_options)
            validation_errors = _validation_error_texts_for_entry(
                validation_base,
                entry,
                full_options,
            )
            if validation_errors:
                rejected.append({
                    "cashier_id": cashier_id,
                    "reason": f"Transaction validation failed: {'; '.join(validation_errors)}",
                })
                continue

            seen_ids_in_request[cashier_id] = entry
            valid_new.append(entry)
            accepted_new_ids.append(cashier_id)

        if not valid_new and not source_id_matches:
            return (
                {
                    "synchronized": _unique_ids(synchronized),
                    "rejected": rejected,
                },
                wrote,
            )

        candidate = _insert_cashier_ids_into_source(existing_content, source_id_matches)
        candidate = _append_entries_to_source(candidate, valid_new, full_options)

        validation_errors = _validate_candidate_as_full_ledger(candidate, full_options)
        if validation_errors:
            reason = f"Candidate file validation failed: {'; '.join(validation_errors)}"
            for _, cashier_id in source_id_matches:
                rejected.append({"cashier_id": cashier_id, "reason": reason})
            for cashier_id in accepted_new_ids:
                rejected.append({"cashier_id": cashier_id, "reason": reason})
            return (
                {
                    "synchronized": _unique_ids(synchronized),
                    "rejected": rejected,
                },
                wrote,
            )

        logger.info(
            "Writing {} new transaction(s) to {}",
            len(valid_new), manual_file,
        )
        _write_file_crash_conscious(manual_file, existing_content, candidate)
        wrote = True
        synchronized.extend(cashier_id for _, cashier_id in source_id_matches)
        synchronized.extend(accepted_new_ids)

    return (
        {
            "synchronized": _unique_ids(synchronized),
            "rejected": rejected,
        },
        wrote,
    )
