"""Security regressions for the single existing, bind-mounted write target."""

import hashlib
import multiprocessing
import os
from pathlib import Path

import pytest
from beancount import loader

import writeback


def transaction(cashier_id="security-1", *, amount="5.00", narration="Security"):
    return (
        f'2026-05-28 * "{narration}"\n'
        f'  cashier_id: "{cashier_id}"\n'
        f'  Assets:Cash -{amount} USD\n'
        f'  Equity:Opening-Balances {amount} USD\n'
    )


def assert_rejected(configured_env, text):
    manual = Path(configured_env)
    before = manual.read_bytes()
    result, wrote = writeback.validate_and_commit([text])
    assert not wrote
    assert result["synchronized"] == []
    assert result["rejected"]
    assert manual.read_bytes() == before
    return result["rejected"][0]["reason"]


@pytest.mark.parametrize("control", [
    'option "title" "Injected"',
    'option "name_assets" "Funds"',
    'include "secret.bean"',
    'plugin "os"',
    'pushtag #injected\npoptag #injected',
    'poptag #injected',
    'pushmeta injected: "value"\npopmeta injected:',
    'popmeta injected:',
])
@pytest.mark.parametrize("before", [True, False])
def test_parser_control_directives_never_disappear(configured_env, control, before):
    text = control + "\n" + transaction() if before else transaction() + control + "\n"
    assert "control directives" in assert_rejected(configured_env, text)


@pytest.mark.parametrize("directive", [
    "2026-05-28 open Assets:Other USD",
    "2026-05-28 close Assets:Cash",
    "2026-05-28 commodity EUR",
    "2026-05-28 balance Assets:Cash 0 USD",
    "2026-05-28 pad Assets:Cash Equity:Opening-Balances",
    '2026-05-28 event "location" "Elsewhere"',
    '2026-05-28 query "query" "SELECT 1"',
    "2026-05-28 price USD 1 EUR",
    '2026-05-28 note Assets:Cash "Note"',
    '2026-05-28 document Assets:Cash "private.pdf"',
    '2026-05-28 custom "custom" "value"',
])
def test_all_nontransaction_directives_rejected(configured_env, directive):
    assert_rejected(configured_env, directive + "\n")
    assert_rejected(configured_env, transaction() + directive + "\n")


def test_directive_words_in_strings_and_comments_are_not_controls(configured_env):
    text = '; option "title" "comment"\n' + transaction(narration="include plugin pushmeta poptag")
    result, wrote = writeback.validate_and_commit([text])
    assert wrote
    assert result == {"synchronized": ["security-1"], "rejected": []}


@pytest.mark.parametrize("cashier_id", [
    "", " leading", "trailing ", "embedded space", "tab\tkey", "line\nkey",
    "control\x01key", "delete\x7fkey", "bidi\u202ekey", "nonbreaking\u00a0key",
    "a" * 129, "é", ".leading", "../path",
])
def test_cashier_id_is_bounded_and_canonical(configured_env, cashier_id):
    assert_rejected(configured_env, transaction(cashier_id))


@pytest.mark.parametrize("cashier_id", ["a", "a" * 128, "UUID-123_abc.def:456"])
def test_canonical_cashier_ids_remain_supported(configured_env, cashier_id):
    result, wrote = writeback.validate_and_commit([transaction(cashier_id)])
    assert wrote
    assert result["synchronized"] == [cashier_id]


def test_transaction_count_bound_precedes_file_access(monkeypatch):
    monkeypatch.delenv("BEANCOUNT_MANUAL_TRANSACTIONS_FILE", raising=False)
    with pytest.raises(ValueError, match="100"):
        writeback.validate_and_commit([transaction()] * 101)


def test_transaction_text_byte_limit(configured_env):
    # Unicode must be bounded as encoded bytes, not Python character count.
    assert "65536 bytes" in assert_rejected(configured_env, transaction(narration="é" * 32768))


@pytest.mark.parametrize("count, accepted", [(256, True), (257, False)])
def test_posting_limit(configured_env, count, accepted):
    text = '2026-05-28 * "Postings"\n  cashier_id: "postings"\n' + "  Assets:Cash 0 USD\n" * count
    result, wrote = writeback.validate_and_commit([text])
    assert wrote is accepted
    assert bool(result["synchronized"]) is accepted
    assert bool(result["rejected"]) is not accepted


@pytest.mark.parametrize("count, accepted", [(63, True), (64, False)])
@pytest.mark.parametrize("posting_metadata", [False, True])
def test_metadata_limit_includes_transaction_and_posting_metadata(configured_env, count, accepted, posting_metadata):
    fields = "".join(f'  field{index}: "value"\n' for index in range(count))
    text = transaction()
    if posting_metadata:
        text += fields.replace("  field", "    field")
    else:
        text = text.replace("  Assets:Cash", fields + "  Assets:Cash")
    result, wrote = writeback.validate_and_commit([text])
    assert wrote is accepted
    assert bool(result["synchronized"]) is accepted


def test_source_metadata_limit_counts_reserved_location_keys(configured_env):
    fields = "".join(f'  field{index}: "value"\n' for index in range(63))
    fields += '  filename: "untrusted"\n'
    text = transaction().replace("  Assets:Cash", fields + "  Assets:Cash")
    assert "64 metadata" in assert_rejected(configured_env, text)


@pytest.mark.parametrize("includes", [
    "",
    'include "manual_transactions.bean"\ninclude "manual_transactions.bean"\n',
    'include "*.bean"\n',
])
def test_actual_manual_candidate_must_be_included_exactly_once(configured_env, includes):
    manual = Path(configured_env)
    root = manual.with_name("main.bean")
    root.write_text('2024-01-01 open Assets:Cash USD\n2024-01-01 open Equity:Opening-Balances USD\n' + includes)
    with pytest.raises(ValueError):
        writeback.validate_and_commit([transaction()])
    assert manual.read_bytes() == b""


def test_overlapping_glob_includes_cannot_hide_duplicate_manual(configured_env):
    root = Path(configured_env).with_name("main.bean")
    with root.open("a") as stream:
        stream.write('include "manual*.bean"\n')
    with pytest.raises(ValueError, match="exactly once"):
        writeback.validate_and_commit([transaction()])
    assert Path(configured_env).read_bytes() == b""


@pytest.mark.parametrize("include", ['include "manu*.bean"', 'include "manual\\_transactions.bean"'])
def test_glob_and_decoded_includes_validate_actual_override(configured_env, include):
    root = Path(configured_env).with_name("main.bean")
    root.write_text(root.read_text().replace('include "manual_transactions.bean"', include))
    unbalanced = transaction().replace("5.00 USD\n", "4.00 USD\n", 1)
    assert "validation failed" in assert_rejected(configured_env, unbalanced)


def test_authoritative_loader_errors_fail_closed(configured_env):
    root = Path(configured_env).with_name("main.bean")
    with root.open("a") as stream:
        stream.write("2026-05-29 balance Assets:Cash 123456 USD\n")
    with pytest.raises(RuntimeError, match="Authoritative full ledger has errors"):
        writeback.validate_and_commit([transaction()])
    assert Path(configured_env).read_bytes() == b""


@pytest.mark.parametrize("existing", [False, True])
def test_final_failure_never_synchronizes_pending_duplicates(configured_env, monkeypatch, existing):
    if existing:
        writeback.validate_and_commit([transaction("durable")])
    before = Path(configured_env).read_bytes()
    monkeypatch.setattr(writeback, "_validation_error_texts_for_entry", lambda *args: [])
    monkeypatch.setattr(writeback, "_validate_candidate_as_full_ledger", lambda *args: ["final failure"])
    texts = [transaction(), transaction()]
    if existing:
        texts.insert(0, transaction("durable"))
    result, wrote = writeback.validate_and_commit(texts)
    assert not wrote
    assert result["synchronized"] == (["durable"] if existing else [])
    assert result["rejected"] == [{
        "cashier_id": "security-1", "reason": "Candidate file validation failed: final failure",
    }]
    assert Path(configured_env).read_bytes() == before


@pytest.mark.parametrize("symlink_parent", [False, True])
def test_symlink_target_or_ancestor_rejected(configured_env, monkeypatch, symlink_parent):
    manual = Path(configured_env)
    before = manual.read_bytes()
    link = manual.parent.with_name(manual.parent.name + "-link")
    if symlink_parent:
        link.symlink_to(manual.parent, target_is_directory=True)
        monkeypatch.setenv("BEANCOUNT_MANUAL_TRANSACTIONS_FILE", str(link / manual.name))
    else:
        target = manual.with_name("other.bean")
        manual.rename(target)
        manual.symlink_to(target)
    try:
        with pytest.raises((OSError, RuntimeError)):
            writeback.validate_and_commit([transaction()])
        assert manual.read_bytes() == before
    finally:
        if symlink_parent:
            link.unlink()


@pytest.mark.parametrize("kind", ["missing", "directory", "fifo", "hardlink"])
def test_only_existing_single_link_regular_file_is_writable(configured_env, kind):
    manual = Path(configured_env)
    manual.unlink()
    if kind == "directory":
        manual.mkdir()
    elif kind == "fifo":
        os.mkfifo(manual)
    elif kind == "hardlink":
        manual.write_bytes(b"")
        os.link(manual, manual.with_name("alias.bean"))
    with pytest.raises((OSError, RuntimeError)):
        writeback.validate_and_commit([transaction()])
    if kind == "missing":
        assert not manual.exists()


def test_path_replacement_during_validation_is_not_written(configured_env, monkeypatch):
    manual = Path(configured_env)
    original = writeback._validate_candidate_as_full_ledger
    calls = 0

    def replace_after_validation(*args):
        nonlocal calls
        errors = original(*args)
        calls += 1
        if calls == 2:  # per-entry then final validation
            manual.rename(manual.with_name("original.bean"))
            manual.write_bytes(b"; substituted\n")
        return errors

    monkeypatch.setattr(writeback, "_validate_candidate_as_full_ledger", replace_after_validation)
    with pytest.raises(RuntimeError, match="identity changed"):
        writeback.validate_and_commit([transaction()])
    assert manual.read_bytes() == b"; substituted\n"
    assert manual.with_name("original.bean").read_bytes() == b""


def test_path_replaced_during_append_never_reports_synchronized(configured_env, monkeypatch):
    manual = Path(configured_env)
    original_write = writeback._write_file_crash_conscious

    def replace_during_append(*args):
        original_write(*args)
        manual.rename(manual.with_name("detached.bean"))
        manual.write_bytes(b"; replacement during append\n")

    monkeypatch.setattr(writeback, "_write_file_crash_conscious", replace_during_append)
    with pytest.raises(RuntimeError, match="identity changed"):
        writeback.validate_and_commit([transaction()])
    assert manual.read_bytes() == b"; replacement during append\n"


def test_uncooperative_content_change_during_validation_fails_closed(configured_env, monkeypatch):
    manual = Path(configured_env)
    original = writeback._validate_candidate_as_full_ledger
    calls = 0

    def mutate_after_validation(*args):
        nonlocal calls
        errors = original(*args)
        calls += 1
        if calls == 2:
            manual.write_bytes(b"; external edit\n")
        return errors

    monkeypatch.setattr(writeback, "_validate_candidate_as_full_ledger", mutate_after_validation)
    with pytest.raises(RuntimeError, match="changed during validation"):
        writeback.validate_and_commit([transaction()])
    assert manual.read_bytes() == b"; external edit\n"


@pytest.mark.parametrize("failure", ["write", "fsync", "zero_write"])
def test_catchable_partial_append_rolls_back_exact_bytes(configured_env, monkeypatch, failure):
    manual = Path(configured_env)
    previous = "; Unicode café with CRLF\r\n".encode()
    manual.write_bytes(previous)
    original_write = os.write
    original_fsync = os.fsync
    failed = False
    writes = 0
    syncs = 0

    def flaky_write(fd, content):
        nonlocal failed, writes
        writes += 1
        if writes == 2:
            if failure == "write":
                original_write(fd, content[:23])
                failed = True
                raise OSError("injected partial append")
            if failure == "zero_write":
                failed = True
                return 0
        return original_write(fd, content)

    def flaky_fsync(fd):
        nonlocal failed, syncs
        syncs += 1
        if failure == "fsync" and syncs == 2:
            failed = True
            raise OSError("injected fsync failure")
        return original_fsync(fd)

    monkeypatch.setattr(os, "write", flaky_write)
    monkeypatch.setattr(os, "fsync", flaky_fsync)
    with pytest.raises(OSError):
        writeback.validate_and_commit([transaction()])
    assert failed
    assert syncs >= (3 if failure == "fsync" else 2)
    assert manual.read_bytes() == previous
    monkeypatch.setattr(os, "write", original_write)
    monkeypatch.setattr(os, "fsync", original_fsync)
    result, wrote = writeback.validate_and_commit([transaction()])
    assert wrote
    assert result["synchronized"] == ["security-1"]
    assert manual.read_bytes().startswith(previous)


def test_failed_recovery_poisoned_worker_and_restart_detects_suffix(configured_env, monkeypatch):
    manual = Path(configured_env)
    original_write = os.write
    original_truncate = os.ftruncate
    writes = 0

    def flaky_write(fd, content):
        nonlocal writes
        writes += 1
        if writes == 2:
            original_write(fd, content[:13])
            raise OSError("partial append")
        return original_write(fd, content)

    def failing_truncate(*args):
        raise OSError("cannot recover")

    monkeypatch.setattr(os, "write", flaky_write)
    monkeypatch.setattr(os, "ftruncate", failing_truncate)
    try:
        with pytest.raises(RuntimeError, match="append and recovery failed"):
            writeback.validate_and_commit([transaction()])
        damaged = manual.read_bytes()
        with pytest.raises(RuntimeError, match="recovery previously failed"):
            writeback.validate_and_commit([transaction()])
        assert manual.read_bytes() == damaged
        writeback._recovery_failures.clear()  # simulate a fresh worker
        monkeypatch.setattr(os, "write", original_write)
        monkeypatch.setattr(os, "ftruncate", original_truncate)
        with pytest.raises(RuntimeError, match="operator recovery"):
            writeback.validate_and_commit([transaction()])
        assert manual.read_bytes() == damaged
    finally:
        writeback._recovery_failures.clear()


@pytest.mark.parametrize("suffix", [
    b";", b"; cash", b"; cashier-writeback", b"; cashier-writeback-v1",
    b"; cashier-writeback-v1 beg",
    b"; cashier-writeback-v1 begin " + b"0" * 64,
    b"; cashier-writeback-v1 begin " + b"0" * 64 + b"\n2026-05-28",
])
def test_restart_incomplete_suffix_is_preserved_not_discarded(configured_env, suffix):
    manual = Path(configured_env)
    manual.write_bytes(suffix)
    with pytest.raises(RuntimeError, match="operator recovery"):
        writeback.validate_and_commit([transaction()])
    assert manual.read_bytes() == suffix


def test_restart_rejects_syntactically_valid_incomplete_batch(configured_env):
    manual = Path(configured_env)
    complete = writeback._append_entries_to_source("", writeback._parse_transaction(transaction())[0]).encode()
    truncated = complete[:complete.index(b"; cashier-writeback-v1 end ")]
    manual.write_bytes(truncated)
    with pytest.raises(RuntimeError, match="Incomplete writeback batch"):
        writeback.validate_and_commit([transaction("next")])
    assert manual.read_bytes() == truncated


def test_restart_rejects_batch_checksum_mismatch(configured_env):
    writeback.validate_and_commit([transaction()])
    manual = Path(configured_env)
    damaged = manual.read_bytes().replace(b"Security", b"Tampered")
    manual.write_bytes(damaged)
    with pytest.raises(RuntimeError, match="Corrupt writeback batch"):
        writeback.validate_and_commit([transaction("next")])
    assert manual.read_bytes() == damaged


def test_flock_unavailable_is_configuration_failure(configured_env, monkeypatch):
    monkeypatch.setattr(writeback, "fcntl", None)
    with pytest.raises(RuntimeError, match="Windows is unsupported"):
        writeback.validate_and_commit([transaction()])
    assert Path(configured_env).read_bytes() == b""


def _process_writer(text, entered, acquired, release, results):
    """Spawned workers get independent interpreter state and process-local locks."""
    original = writeback._capture_workspace

    def paused_capture(*args):
        acquired.set()
        if release is not None and not release.wait(15):
            raise RuntimeError("test timed out waiting to release locked worker")
        return original(*args)

    writeback._capture_workspace = paused_capture
    entered.set()
    try:
        results.put(("ok", writeback.validate_and_commit([text])))
    except (OSError, RuntimeError, ValueError) as exc:
        results.put(("error", repr(exc)))


@pytest.mark.parametrize("mode", ["duplicate", "conflict", "distinct"])
def test_multiprocess_flock_spans_read_dedupe_validation_and_append(configured_env, mode):
    ctx = multiprocessing.get_context("spawn")
    release = ctx.Event()
    entered1, entered2 = ctx.Event(), ctx.Event()
    acquired1, acquired2 = ctx.Event(), ctx.Event()
    results = ctx.Queue()
    second_id = "security-2" if mode == "distinct" else "security-1"
    second_text = transaction(second_id, amount="7.00" if mode == "conflict" else "5.00")
    workers = [
        ctx.Process(target=_process_writer, args=(transaction(), entered1, acquired1, release, results)),
        ctx.Process(target=_process_writer, args=(second_text, entered2, acquired2, None, results)),
    ]
    try:
        workers[0].start()
        assert acquired1.wait(10)
        workers[1].start()
        assert entered2.wait(10)
        assert not acquired2.wait(0.4), "second process read the manual file while the first held its lock"
        release.set()
        outcomes = [results.get(timeout=20), results.get(timeout=20)]
        assert all(kind == "ok" for kind, _ in outcomes), outcomes
        responses = [result for _, (result, _) in outcomes]
        if mode == "conflict":
            assert sum(bool(result["rejected"]) for result in responses) == 1
            rejected = next(result for result in responses if result["rejected"])
            assert rejected["synchronized"] == []
            assert "Conflict" in rejected["rejected"][0]["reason"]
        else:
            assert all(result["rejected"] == [] for result in responses)
        assert sorted(wrote for _, (_, wrote) in outcomes) == ([True, True] if mode == "distinct" else [False, True])
        for worker in workers:
            worker.join(10)
            assert worker.exitcode == 0
    finally:
        release.set()
        for worker in workers:
            if worker.pid is not None:
                if worker.is_alive():
                    worker.terminate()
                worker.join(5)
        results.close()
        results.join_thread()
    content = Path(configured_env).read_text()
    assert content.count('cashier_id: "security-1"') == 1
    assert content.count('cashier_id: "security-2"') == (1 if mode == "distinct" else 0)
    if mode == "conflict":
        assert "7.00 USD" not in content
    _, errors, _ = loader.load_file(str(Path(configured_env).with_name("main.bean")))
    assert not errors
    writeback._check_writeback_integrity(Path(configured_env).read_bytes())


def _interrupted_writer(ready):
    original_write_all = writeback._write_all
    calls = 0

    def interrupt_after_durable_intent(fd, content):
        nonlocal calls
        calls += 1
        if calls == 2:
            ready.set()
            # The test terminates this process after begin+fsync, before payload.
            multiprocessing.Event().wait(30)
        original_write_all(fd, content)

    writeback._write_all = interrupt_after_durable_intent
    writeback.validate_and_commit([transaction()])


def test_process_interruption_requires_operator_recovery(configured_env):
    ctx = multiprocessing.get_context("spawn")
    ready = ctx.Event()
    worker = ctx.Process(target=_interrupted_writer, args=(ready,))
    try:
        worker.start()
        assert ready.wait(10)
        worker.terminate()
        worker.join(10)
        assert worker.exitcode is not None and worker.exitcode != 0
        damaged = Path(configured_env).read_bytes()
        assert damaged.startswith(b"; cashier-writeback-v1 begin ")
        assert b"cashier_id:" not in damaged
        with pytest.raises(RuntimeError, match="Incomplete writeback batch"):
            writeback.validate_and_commit([transaction("retry")])
        assert Path(configured_env).read_bytes() == damaged
    finally:
        if worker.is_alive():
            worker.terminate()
        worker.join(5)


def test_short_writes_complete_on_same_descriptor(configured_env, monkeypatch):
    original_write = os.write
    calls = 0

    def short_write(fd, content):
        nonlocal calls
        calls += 1
        return original_write(fd, content[:7])

    monkeypatch.setattr(os, "write", short_write)
    result, wrote = writeback.validate_and_commit([transaction()])
    assert wrote
    assert result == {"synchronized": ["security-1"], "rejected": []}
    assert calls > 10
    writeback._check_writeback_integrity(Path(configured_env).read_bytes())


def test_manual_file_cannot_be_the_ledger_root(configured_env, monkeypatch):
    monkeypatch.setenv("BEANCOUNT_MANUAL_TRANSACTIONS_FILE", os.environ["BEANCOUNT_FILE"])
    before = Path(os.environ["BEANCOUNT_FILE"]).read_bytes()
    with pytest.raises(ValueError, match="not the ledger root"):
        writeback.validate_and_commit([transaction()])
    assert Path(os.environ["BEANCOUNT_FILE"]).read_bytes() == before


def test_batch_markers_are_comments_with_exact_payload_digest(configured_env):
    writeback.validate_and_commit([transaction()])
    content = Path(configured_env).read_bytes()
    begin, body = content.split(b"\n", 1)
    body, end = body.split(b"; cashier-writeback-v1 end ")
    assert begin.endswith(hashlib.sha256(body).hexdigest().encode())
    assert end.strip() == hashlib.sha256(body).hexdigest().encode()


@pytest.mark.parametrize("posting_metadata", [False, True])
@pytest.mark.parametrize("value", [
    "x" * 1025, "é" * 513, "tab\tvalue", "line\nvalue",
    "control\x01value", "delete\x7fvalue", "bidi\u202evalue", "e\u0301",
])
def test_metadata_strings_are_bounded_printable_and_canonical(configured_env, posting_metadata, value):
    fields = f'  diagnostic: "{value}"\n'
    text = transaction()
    if posting_metadata:
        text += "  " + fields
    else:
        text = text.replace("  Assets:Cash", fields + "  Assets:Cash")
    assert "Metadata strings" in assert_rejected(configured_env, text)


@pytest.mark.parametrize("value", ["", "café", "é" * 512])
def test_printable_metadata_within_byte_bound_is_preserved(configured_env, value):
    text = transaction().replace("  Assets:Cash", f'  diagnostic: "{value}"\n  Assets:Cash')
    result, wrote = writeback.validate_and_commit([text])
    assert wrote
    assert result["rejected"] == []
    entries, errors, _ = writeback.parser.parse_string(Path(configured_env).read_bytes())
    assert not errors
    assert entries[0].meta["diagnostic"] == value


@pytest.mark.parametrize("key", ["field" * 13, "filename", "lineno"])
def test_reserved_or_oversized_metadata_names_rejected(configured_env, key):
    text = transaction().replace("  Assets:Cash", f'  {key}: "value"\n  Assets:Cash')
    assert_rejected(configured_env, text)


def test_multiline_strings_cannot_be_mistaken_for_control_directives(configured_env):
    text = transaction(narration="first line\ninclude plugin option pushtag\nlast line")
    result, wrote = writeback.validate_and_commit([text])
    assert wrote
    assert result == {"synchronized": ["security-1"], "rejected": []}
    retry, wrote = writeback.validate_and_commit([text])
    assert not wrote
    assert retry == result


def test_recovery_marker_in_multiline_string_cannot_poison_committed_file(configured_env):
    text = transaction(narration="first line\n; cashier-writeback-v1 begin " + "0" * 64 + "\nlast line")
    assert_rejected(configured_env, text)


@pytest.mark.parametrize("kind", ["absolute", "traversal", "symlink", "symlink_directory"])
def test_unsafe_manual_include_graph_fails_closed(configured_env, kind):
    manual = Path(configured_env)
    root = manual.with_name("main.bean")
    include = str(manual)
    if kind == "traversal":
        include = f"../{manual.parent.name}/{manual.name}"
    elif kind == "symlink":
        alias = manual.with_name("alias.bean")
        alias.symlink_to(manual.name)
        include = alias.name
    elif kind == "symlink_directory":
        alias = manual.with_name("alias")
        alias.symlink_to(manual.parent, target_is_directory=True)
        include = f"{alias.name}/{manual.name}"
    root.write_text(root.read_text().replace('include "manual_transactions.bean"', f'include "{include}"'))
    with pytest.raises((ValueError, OSError)):
        writeback.validate_and_commit([transaction()])
    assert manual.read_bytes() == b""


def test_nested_relative_include_loads_actual_candidate(configured_env):
    manual = Path(configured_env)
    root = manual.with_name("main.bean")
    bridge = manual.with_name("bridge.bean")
    bridge.write_text('include "manual_transactions.bean"\n')
    root.write_text(root.read_text().replace('include "manual_transactions.bean"', 'include "bridge.bean"'))
    invalid = transaction().replace("5.00 USD\n", "4.00 USD\n", 1)
    assert "validation failed" in assert_rejected(configured_env, invalid)
    result, wrote = writeback.validate_and_commit([transaction()])
    assert wrote
    assert result["synchronized"] == ["security-1"]


def test_one_capture_and_materialization_per_batch_preserves_plugin_partial_success(configured_env, monkeypatch):
    from cashier_snapshot.workspace import LedgerWorkspace

    root = Path(configured_env).with_name("main.bean")
    root.write_text(root.read_text() + 'plugin "beancount.plugins.noduplicates"\n')
    original_capture = writeback._capture_workspace
    original_private_copy = LedgerWorkspace.private_copy
    captures = []
    materializations = []

    def capture(*args):
        captures.append(args)
        return original_capture(*args)

    def private_copy(self, *args, **kwargs):
        materializations.append(args)
        return original_private_copy(self, *args, **kwargs)

    monkeypatch.setattr(writeback, "_capture_workspace", capture)
    monkeypatch.setattr(LedgerWorkspace, "private_copy", private_copy)
    result, wrote = writeback.validate_and_commit([
        transaction("first"), transaction("duplicate"), transaction("last", amount="7.00"),
    ])
    assert wrote
    assert result["synchronized"] == ["first", "last"]
    assert len(result["rejected"]) == 1
    assert result["rejected"][0]["cashier_id"] == "duplicate"
    assert "Duplicate" in result["rejected"][0]["reason"]
    assert len(captures) == len(materializations) == 1
    content = Path(configured_env).read_text()
    assert 'cashier_id: "duplicate"' not in content
    _, errors, _ = loader.load_file(str(root))
    assert not errors


def test_unverified_external_bytes_are_never_truncated_during_rollback(configured_env, monkeypatch):
    manual = Path(configured_env)
    original_write = os.write
    writes = 0

    def append_external_bytes_and_fail(fd, content):
        nonlocal writes
        writes += 1
        if writes == 2:
            original_write(fd, b"; uncooperative external append\n")
            raise OSError("write failed after external edit")
        return original_write(fd, content)

    monkeypatch.setattr(os, "write", append_external_bytes_and_fail)
    try:
        with pytest.raises(RuntimeError, match="append and recovery failed"):
            writeback.validate_and_commit([transaction(), transaction()])
        assert manual.read_bytes().endswith(b"; uncooperative external append\n")
    finally:
        writeback._recovery_failures.clear()


def test_identity_check_rejects_parent_symlink_swap(configured_env, monkeypatch):
    manual = Path(configured_env)
    directory = manual.parent
    detached = directory.with_name(directory.name + "-detached")
    original = writeback._validate_candidate_as_full_ledger
    calls = 0

    def swap_after_validation(*args):
        nonlocal calls
        errors = original(*args)
        calls += 1
        if calls == 2:
            directory.rename(detached)
            directory.symlink_to(detached, target_is_directory=True)
        return errors

    monkeypatch.setattr(writeback, "_validate_candidate_as_full_ledger", swap_after_validation)
    try:
        with pytest.raises(OSError):
            writeback.validate_and_commit([transaction()])
        assert (detached / manual.name).read_bytes() == b""
    finally:
        if directory.is_symlink():
            directory.unlink()
            detached.rename(directory)


def test_manual_read_has_a_bound_before_capture(configured_env, monkeypatch):
    manual = Path(configured_env)
    previous = b"; existing manual source\n"
    manual.write_bytes(previous)
    import cashier_snapshot.workspace as workspace_module

    monkeypatch.setattr(workspace_module, "MAX_FILE_BYTES", len(previous) - 1)
    with pytest.raises(RuntimeError, match="source byte budget"):
        writeback.validate_and_commit([transaction()])
    assert manual.read_bytes() == previous


def test_loader_must_report_actual_staged_manual_input(configured_env, monkeypatch):
    original_load = loader._uncached_load_file

    def omit_manual(*args, **kwargs):
        entries, errors, options = original_load(*args, **kwargs)
        options["include"] = [path for path in options["include"] if not path.endswith("manual_transactions.bean")]
        return entries, errors, options

    monkeypatch.setattr(loader, "_uncached_load_file", omit_manual)
    with pytest.raises(RuntimeError, match="did not load the staged manual file exactly once"):
        writeback.validate_and_commit([transaction()])
    assert Path(configured_env).read_bytes() == b""


def test_validation_never_uses_cached_loader_result(configured_env, monkeypatch):
    def cached_load_forbidden(*args, **kwargs):
        raise AssertionError("Writeback must bypass the cached loader")

    monkeypatch.setattr(loader, "_load_file", cached_load_forbidden)
    invalid = transaction().replace("5.00 USD\n", "4.00 USD\n", 1)
    assert "validation failed" in assert_rejected(configured_env, invalid)
    result, wrote = writeback.validate_and_commit([transaction()])
    assert wrote
    assert result["synchronized"] == ["security-1"]


def test_pending_duplicates_are_not_acknowledged_when_append_fails(configured_env, monkeypatch):
    def fail_append(*args):
        raise OSError("durable append failed")

    monkeypatch.setattr(writeback, "_write_file_crash_conscious", fail_append)
    with pytest.raises(OSError, match="durable append failed"):
        writeback.validate_and_commit([transaction(), transaction()])
    assert Path(configured_env).read_bytes() == b""


@pytest.mark.parametrize("label", ['ordinary "quote"', "ordinary\\label", ""])
def test_unsafe_lot_label_rendering_rejected_before_candidate_loading(configured_env, monkeypatch, label):
    quoted = '"' + label.replace("\\", "\\\\").replace('"', '\\"') + '"'
    text = transaction().replace("-5.00 USD", f'-5.00 USD {{1 USD, {quoted}}}')
    entries, errors, _ = writeback._parse_transaction(text)
    assert not errors
    assert len(entries) == 1

    def candidate_load_forbidden(*args):
        raise AssertionError("Unsafe serialization reached candidate loading")

    monkeypatch.setattr(writeback, "_validate_candidate_as_full_ledger", candidate_load_forbidden)
    assert "serialization must preserve" in assert_rejected(configured_env, text)


@pytest.mark.parametrize("alteration", ["amount", "second_transaction", "control"])
def test_every_rendered_block_is_restricted_and_semantically_checked(configured_env, monkeypatch, alteration):
    original = writeback.EntryPrinter

    class ChangedPrinter:
        def __init__(self, *args, **kwargs):
            self.printer = original(*args, **kwargs)

        def __call__(self, entry):
            rendered = self.printer(entry)
            if alteration == "amount":
                return rendered.replace("5.00 USD", "4.00 USD")
            if alteration == "second_transaction":
                return rendered + "\n" + transaction("second")
            return rendered + '\noption "title" "ordinary title"\n'

    monkeypatch.setattr(writeback, "EntryPrinter", ChangedPrinter)
    assert "serialization must preserve" in assert_rejected(configured_env, transaction())


@pytest.mark.parametrize("pattern", ["/not-a-ledger-source.bean", "../outside.bean", "extra*.bean"])
def test_candidate_graph_revalidated_before_loading(configured_env, monkeypatch, pattern):
    from cashier_snapshot.workspace import LedgerWorkspaceError

    manual = Path(configured_env)
    workspace, relative = writeback._capture_workspace(str(manual), b"")
    with writeback._private_validation_root(workspace, relative) as validation:
        previous = validation.manual.read()
        validation.content = (transaction() + f'include "{pattern}"\n').encode()

        def loader_forbidden(*args):
            raise AssertionError("Unvalidated include graph reached loader")

        monkeypatch.setattr(loader, "_uncached_load_file", loader_forbidden)
        with pytest.raises(LedgerWorkspaceError):
            writeback._load_workspace(validation, relative)
        validation.manual.seek(0)
        assert validation.manual.read() == previous
    assert manual.read_bytes() == b""


def test_full_candidate_graph_checked_before_each_loader_call(configured_env, monkeypatch):
    from cashier_snapshot.workspace import LedgerWorkspace

    original_check = LedgerWorkspace._candidate_files
    original_load = loader._uncached_load_file
    checks = []
    loads = []

    def check(self, overrides):
        files = original_check(self, overrides)
        checks.append(files[Path("manual_transactions.bean")])
        return files

    def load(path, *args):
        candidate = (Path(path).parent / "manual_transactions.bean").read_bytes()
        assert checks[-1] == candidate
        assert len(checks) > len(loads)
        loads.append(candidate)
        return original_load(path, *args)

    monkeypatch.setattr(LedgerWorkspace, "_candidate_files", check)
    monkeypatch.setattr(loader, "_uncached_load_file", load)
    result, wrote = writeback.validate_and_commit([transaction(), transaction("second", amount="7.00")])
    assert wrote
    assert result["rejected"] == []
    assert len(loads) == 4  # baseline, each entry, final combined candidate
    assert loads[-1] == Path(configured_env).read_bytes()


def test_temp_ancestor_swap_cannot_redirect_candidate_writes(configured_env, monkeypatch):
    manual = Path(configured_env)
    temporary_root = manual.parent.with_name(manual.parent.name + "-temp")
    moved = temporary_root.with_name(temporary_root.name + "-moved")
    temporary_root.mkdir()
    monkeypatch.setenv("TMPDIR", str(temporary_root))
    workspace, relative = writeback._capture_workspace(str(manual), b"")
    with writeback._private_validation_root(workspace, relative) as validation:
        original_fd_identity = writeback._file_identity(validation.manual.fileno())
        temporary_root.rename(moved)
        temporary_root.symlink_to(manual.parent, target_is_directory=True)
        assert str(validation.root).startswith("/proc/self/fd/")
        candidate = writeback._append_entries_to_source("", writeback._parse_transaction(transaction())[0])
        assert writeback._validate_candidate_as_full_ledger(candidate, validation, relative) == []
        assert writeback._file_identity(validation.manual.fileno()) == original_fd_identity
        assert (validation.root.parent / relative).read_bytes() == candidate.encode()
        assert manual.read_bytes() == b""
    assert list(moved.iterdir()) == []
    assert sorted(path.name for path in manual.parent.iterdir()) == ["main.bean", "manual_transactions.bean"]


@pytest.mark.parametrize("symlink", [False, True])
def test_unsafe_temp_storage_never_probes_or_creates_in_ledger(configured_env, monkeypatch, symlink):
    import tempfile

    manual = Path(configured_env)
    temporary_root = manual.parent
    if symlink:
        temporary_root = manual.parent.with_name(manual.parent.name + "-temp-alias")
        temporary_root.symlink_to(manual.parent, target_is_directory=True)
    monkeypatch.setenv("TMPDIR", str(temporary_root))
    before = {path.name: path.read_bytes() for path in manual.parent.iterdir()}

    def probe_forbidden():
        raise AssertionError("Temporary storage must not call gettempdir()")

    monkeypatch.setattr(tempfile, "gettempdir", probe_forbidden)
    with pytest.raises(ValueError):
        writeback.validate_and_commit([transaction()])
    assert {path.name: path.read_bytes() for path in manual.parent.iterdir()} == before


@pytest.mark.parametrize("headroom, accepted", [(0, True), (-1, False)])
def test_candidate_file_limit_includes_checksum_markers(configured_env, monkeypatch, headroom, accepted):
    import cashier_snapshot.workspace as workspace_module

    manual = Path(configured_env)
    limit = 2048
    text = transaction()
    rendered = writeback._append_entries_to_source("", writeback._parse_transaction(text)[0]).encode()
    # A nonempty prefix adds one separator newline in addition to the markers.
    prefix_size = limit - len(rendered) - 1 - headroom
    previous = b";" + b"p" * (prefix_size - 2) + b"\n"
    manual.write_bytes(previous)
    monkeypatch.setattr(workspace_module, "MAX_FILE_BYTES", limit)
    expected = writeback._append_entries_to_source(previous.decode(), writeback._parse_transaction(text)[0]).encode()
    assert len(expected) == limit - headroom
    result, wrote = writeback.validate_and_commit([text])
    assert wrote is accepted
    if not accepted:
        assert "per-file byte budget" in result["rejected"][0]["reason"]
        assert manual.read_bytes() == previous
        return
    assert result == {"synchronized": ["security-1"], "rejected": []}
    assert manual.read_bytes() == expected
    captured = workspace_module.LedgerWorkspace.capture(manual.with_name("main.bean"))
    assert captured.files[Path(manual.name)] == expected
    retry, wrote = writeback.validate_and_commit([text])
    assert not wrote
    assert retry == result
    assert manual.read_bytes() == expected


@pytest.mark.parametrize("budget", ["MAX_WORKSPACE_BYTES", "MAX_EXPANDED_BYTES"])
def test_candidate_aggregate_budgets_checked_before_commit(configured_env, monkeypatch, budget):
    import cashier_snapshot.workspace as workspace_module

    manual = Path(configured_env)
    root = manual.with_name("main.bean")
    rendered = writeback._append_entries_to_source("", writeback._parse_transaction(transaction())[0]).encode()
    limit = len(root.read_bytes()) + len(rendered) - 1
    monkeypatch.setattr(workspace_module, budget, limit)
    reason = assert_rejected(configured_env, transaction())
    assert "byte budget exceeded" in reason


def test_final_candidate_budget_is_checked_after_per_entry_validation(configured_env, monkeypatch):
    import cashier_snapshot.workspace as workspace_module

    monkeypatch.setattr(writeback, "_validation_error_texts_for_entry", lambda *args: [])
    manual = Path(configured_env)
    # Baseline capture fits; the candidate plus both checksum comments does not.
    monkeypatch.setattr(workspace_module, "MAX_FILE_BYTES", 1024)
    previous = b";" + b"p" * 899 + b"\n"
    manual.write_bytes(previous)
    result, wrote = writeback.validate_and_commit([transaction()])
    assert not wrote
    assert result["synchronized"] == []
    assert "per-file byte budget" in result["rejected"][0]["reason"]
    assert manual.read_bytes() == previous
