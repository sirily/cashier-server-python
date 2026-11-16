"""Filesystem boundary regressions using synthetic ledgers only."""

from dataclasses import FrozenInstanceError
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from beancount import loader
from beancount.core import data
from beancount.parser import parser

from cashier_snapshot import SnapshotBuildError, StandaloneSnapshotBuilder
from cashier_snapshot import builder as snapshot_builder
from cashier_snapshot import workspace as workspace_module
from cashier_snapshot.printer import print_generated_entries_for_pwa
from cashier_snapshot.source_index import SourceLedgerIndex
from cashier_snapshot.workspace import (
    LedgerWorkspace,
    LedgerWorkspaceError,
    glob_workspace_files,
    read_workspace_file,
)


@pytest.fixture
def ledger(tmp_path):
    directory = tmp_path / "ledger"
    directory.mkdir()
    root = directory / "main.bean"
    root.write_bytes(b'include "accounts.bean"\n')
    (directory / "accounts.bean").write_bytes(b"2024-01-01 open Assets:Cash USD\n")
    return root


def build(root):
    return StandaloneSnapshotBuilder(root, print_generated_entries_for_pwa).build()


@pytest.mark.parametrize(
    "name", ['quoted"name.bean', "tab\tname.bean", "slash\\name.bean"]
)
def test_includes_use_parser_decoded_names(ledger, name):
    encoded = name.replace("\\", "\\\\").replace('"', '\\"').replace("\t", "\\t")
    ledger.write_text(f'include "{encoded}"\n', encoding="utf-8")
    source = b"2024-01-01 open Assets:Decoded USD\n"
    (ledger.parent / name).write_bytes(source)

    captured = LedgerWorkspace.capture(ledger)
    assert captured.include_counts[Path(name)] == 1
    content, _ = build(ledger)
    assert source.decode() in content
    assert "include " not in content


@pytest.mark.parametrize(
    "pattern", ["../outside.bean", "/outside.bean", r"\..\//outside.bean"]
)
def test_decoded_escaping_includes_rejected(ledger, pattern):
    ledger.write_text(f'include "{pattern}"\n', encoding="utf-8")
    with pytest.raises(LedgerWorkspaceError, match="Unsafe source include path"):
        LedgerWorkspace.capture(ledger)


def test_workspace_is_immutable_and_counts_repeated_includes(ledger):
    ledger.write_bytes(b'include "accounts.bean"\ninclude "accounts.bean"\n')
    captured = LedgerWorkspace.capture(ledger)
    assert captured.root_relative == Path("main.bean")
    assert captured.include_counts == {Path("main.bean"): 1, Path("accounts.bean"): 2}
    captured.files.clear()
    captured.include_counts.clear()
    assert Path("accounts.bean") in captured.files
    assert captured.include_counts[Path("accounts.bean")] == 2
    with pytest.raises(FrozenInstanceError):
        captured.__setattr__("root_relative", Path("changed.bean"))
    with pytest.raises(SnapshotBuildError, match="Repeated included source directive"):
        build(ledger)


def test_nested_glob_capture_and_source_index_agree(ledger):
    (ledger.parent / "nested").mkdir()
    (ledger.parent / "nested" / "deep").mkdir()
    ledger.write_bytes(b'include "nested/**/*.bean"\n')
    (ledger.parent / "nested" / "first.bean").write_bytes(
        b"2024-01-01 open Assets:First USD\n"
    )
    (ledger.parent / "nested" / "deep" / "second.bean").write_bytes(
        b"2024-01-01 open Assets:Second USD\n"
    )
    (ledger.parent / "nested" / ".hidden.bean").write_bytes(b"not source")
    captured = LedgerWorkspace.capture(ledger)
    assert captured.include_counts == {
        Path("main.bean"): 1,
        Path("nested/first.bean"): 1,
        Path("nested/deep/second.bean"): 1,
    }
    assert Path("nested/.hidden.bean") not in captured.files
    with TemporaryDirectory() as directory:
        staged = captured.materialize(Path(directory))
        _, errors, options = loader.load_file(str(staged))
        assert not errors
        assert set(options["include"]) == {
            str(Path(directory) / path) for path in captured.include_counts
        }
    assert len(SourceLedgerIndex.build(ledger).entry_blocks) == 2


def test_capture_includes_unreferenced_nonhidden_bean_files(ledger):
    (ledger.parent / "prices").mkdir()
    prices = b"2024-01-01 price EUR 1.10 USD\n"
    (ledger.parent / "prices" / "latest.bean").write_bytes(prices)
    (ledger.parent / "prices" / "secret.txt").write_bytes(b"not ledger source")
    (ledger.parent / ".hidden").mkdir()
    (ledger.parent / ".hidden" / "hidden.bean").write_bytes(b"not source")
    captured = LedgerWorkspace.capture(ledger)
    assert captured.files[Path("prices/latest.bean")] == prices
    assert Path("prices/latest.bean") not in captured.include_counts
    assert Path("prices/secret.txt") not in captured.files
    assert Path(".hidden/hidden.bean") not in captured.files


def test_explicit_root_filename_exception(ledger):
    root = ledger.rename(ledger.with_name("ledger.beancount"))
    assert LedgerWorkspace.capture(root).root_relative == Path("ledger.beancount")
    assert "Assets:Cash" in build(root)[0]


@pytest.mark.parametrize("target", ["accounts.bean", "nested", "main.bean"])
def test_file_and_directory_symlinks_rejected(ledger, tmp_path, target):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "stolen.bean").write_bytes(b"2024-01-01 open Assets:Secret USD\n")
    link = ledger.parent / target
    if link.exists():
        link.unlink()
    link.symlink_to(outside if target == "nested" else outside / "stolen.bean")
    with pytest.raises(LedgerWorkspaceError, match="Symlink source include"):
        LedgerWorkspace.capture(ledger)


def test_ancestor_symlink_rejected(ledger, tmp_path):
    alias = tmp_path / "alias"
    alias.symlink_to(ledger.parent, target_is_directory=True)
    with pytest.raises(LedgerWorkspaceError, match="Symlink source include"):
        LedgerWorkspace.capture(alias / ledger.name)


def test_regular_file_swap_to_symlink_never_read(ledger, tmp_path, monkeypatch):
    outside = tmp_path / "outside.bean"
    outside.write_bytes(b"2024-01-01 open Assets:Secret USD\n")
    original_open = workspace_module.os.open

    def swapping_open(path, flags, *args, **kwargs):
        if path == "accounts.bean":
            (ledger.parent / "accounts.bean").unlink()
            (ledger.parent / "accounts.bean").symlink_to(outside)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(workspace_module.os, "open", swapping_open)
    with pytest.raises(LedgerWorkspaceError, match="Symlink source include"):
        LedgerWorkspace.capture(ledger)


def test_directory_swap_to_symlink_never_traversed(ledger, tmp_path, monkeypatch):
    nested = ledger.parent / "nested"
    nested.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "stolen.bean").write_bytes(b"2024-01-01 open Assets:Secret USD\n")
    original_open = workspace_module.os.open

    def swapping_open(path, flags, *args, **kwargs):
        if path == "nested":
            nested.rmdir()
            nested.symlink_to(outside, target_is_directory=True)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(workspace_module.os, "open", swapping_open)
    with pytest.raises(LedgerWorkspaceError, match="Symlink source include"):
        LedgerWorkspace.capture(ledger)


def test_file_mutation_during_read_rejected(ledger, monkeypatch):
    original_read = workspace_module.os.read
    changed = False

    def changing_read(descriptor, count):
        nonlocal changed
        content = original_read(descriptor, count)
        if not changed:
            changed = True
            ledger.write_bytes(b"; changed during read\n")
        return content

    monkeypatch.setattr(workspace_module.os, "read", changing_read)
    with pytest.raises(LedgerWorkspaceError, match="changed during capture"):
        LedgerWorkspace.capture(ledger)


def test_fifo_rejected_without_blocking(ledger):
    (ledger.parent / "accounts.bean").unlink()
    workspace_module.os.mkfifo(ledger.parent / "accounts.bean")
    with pytest.raises(LedgerWorkspaceError, match="regular file"):
        LedgerWorkspace.capture(ledger)


@pytest.mark.parametrize("pattern", ["secret.txt", "*.txt", ".hidden.bean", "nested"])
def test_includes_must_target_nonhidden_bean_regular_files(ledger, pattern):
    ledger.write_text(f'include "{pattern}"\n', encoding="utf-8")
    (ledger.parent / "secret.txt").write_text("not source", encoding="utf-8")
    (ledger.parent / ".hidden.bean").write_text("not source", encoding="utf-8")
    (ledger.parent / "nested").mkdir()
    with pytest.raises(LedgerWorkspaceError):
        LedgerWorkspace.capture(ledger)


def test_include_cycles_rejected(ledger):
    (ledger.parent / "accounts.bean").write_bytes(b'include "main.bean"\n')
    with pytest.raises(LedgerWorkspaceError, match="Cyclic include"):
        LedgerWorkspace.capture(ledger)


@pytest.mark.parametrize(
    ("constant", "limit", "message"),
    [
        ("MAX_WORKSPACE_FILES", 1, "file budget"),
        ("MAX_WORKSPACE_BYTES", 1, "byte budget"),
        ("MAX_EXPANDED_BYTES", 1, "expanded source byte budget"),
        ("MAX_INCLUDE_VISITS", 1, "visit budget"),
        ("MAX_INCLUDE_DEPTH", 0, "include depth budget"),
        ("MAX_SCAN_ENTRIES", 1, "scan entry budget"),
        ("MAX_SCAN_DEPTH", 0, "scan depth budget"),
        ("MAX_GLOB_STEPS", 0, "glob expansion budget"),
    ],
)
def test_expansion_budgets_are_enforced(ledger, monkeypatch, constant, limit, message):
    (ledger.parent / "nested").mkdir()
    monkeypatch.setattr(workspace_module, constant, limit)
    with pytest.raises(LedgerWorkspaceError, match=message):
        LedgerWorkspace.capture(ledger)


def test_repeated_include_dag_is_bounded(ledger, monkeypatch):
    ledger.write_bytes(b'include "accounts.bean"\n' * 20)
    monkeypatch.setattr(workspace_module, "MAX_INCLUDE_VISITS", 8)
    with pytest.raises(LedgerWorkspaceError, match="visit budget"):
        LedgerWorkspace.capture(ledger)


def test_materialization_preserves_capture_after_source_replacement(ledger, tmp_path):
    captured = LedgerWorkspace.capture(ledger)
    expected = captured.files
    (ledger.parent / "accounts.bean").unlink()
    (ledger.parent / "accounts.bean").symlink_to(tmp_path / "missing.bean")
    ledger.write_bytes(b'include "/never/read/this.bean"\n')
    staged = captured.materialize(tmp_path / "private")
    assert staged.name == ledger.name
    for relative, content in expected.items():
        assert (staged.parent / relative).read_bytes() == content
    _, errors, _ = loader.load_file(str(staged))
    assert not errors


def test_override_changes_full_ledger_pipeline_without_changing_capture(
    ledger, tmp_path
):
    ledger.write_bytes(
        b'include "accounts.bean"\nplugin "beancount.plugins.auto_accounts"\n'
    )
    captured = LedgerWorkspace.capture(ledger)
    replacement = (
        b'2024-01-02 * "Generated accounts"\n'
        b"  Assets:Generated 1 USD\n  Equity:Generated -1 USD\n"
    )
    staged = captured.materialize(
        tmp_path / "private", {Path("accounts.bean"): replacement}
    )
    entries, errors, _ = loader.load_file(str(staged))
    assert not errors
    assert any(
        isinstance(entry, data.Open) and entry.account == "Assets:Generated"
        for entry in entries
    )
    assert captured.files[Path("accounts.bean")] != replacement
    assert (ledger.parent / "accounts.bean").read_bytes() != replacement


@pytest.mark.parametrize(
    "relative", [Path("../escape.bean"), Path("/escape.bean"), Path("new.bean")]
)
def test_override_cannot_add_paths(ledger, tmp_path, relative):
    with pytest.raises(LedgerWorkspaceError, match="Overrides must replace captured"):
        LedgerWorkspace.capture(ledger).materialize(
            tmp_path / "private", {relative: b"; injected\n"}
        )
    assert not (tmp_path / "private").exists()


def test_override_cannot_add_escaping_includes(ledger, tmp_path):
    with pytest.raises(LedgerWorkspaceError, match="Unsafe source include path"):
        LedgerWorkspace.capture(ledger).materialize(
            tmp_path / "private", {Path("accounts.bean"): b'include "../escape.bean"\n'}
        )
    assert not (tmp_path / "private").exists()


def test_materialization_refuses_source_and_nonempty_directories(ledger, tmp_path):
    captured = LedgerWorkspace.capture(ledger)
    with pytest.raises(LedgerWorkspaceError, match="outside the ledger workspace"):
        captured.materialize(ledger.parent / "stage")
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    (private / "existing.bean").write_bytes(b"do not overwrite")
    with pytest.raises(LedgerWorkspaceError, match="must be empty"):
        captured.materialize(private)
    assert (private / "existing.bean").read_bytes() == b"do not overwrite"


def test_builder_books_and_loads_only_captured_staged_source(ledger, monkeypatch):
    original_baseline = snapshot_builder.load_booked_source_entries
    original_loader = snapshot_builder.loader.load_file
    observed = []

    def baseline(path):
        assert path != ledger
        assert path.parent != ledger.parent
        observed.append(path)
        ledger.write_bytes(b'include "/never/read/this.bean"\n')
        (ledger.parent / "accounts.bean").write_bytes(b"invalid live contents\n")
        return original_baseline(path)

    def load(path):
        assert Path(path) == observed[0]
        observed.append(Path(path))
        return original_loader(path)

    monkeypatch.setattr(snapshot_builder, "load_booked_source_entries", baseline)
    monkeypatch.setattr(snapshot_builder.loader, "load_file", load)
    content, _ = build(ledger)
    assert "2024-01-01 open Assets:Cash USD" in content
    assert len(observed) == 2
    assert not observed[0].exists()


def test_multiline_plugin_config_cannot_invent_includes_or_directives(ledger):
    ledger.write_bytes(
        b'plugin "beancount.plugins.implicit_prices" "\n'
        b'include \\"../not-a-real-include.bean\\"\n'
        b'2024-01-01 open Assets:NotReal USD\n"\n'
        b'include "accounts.bean"\n'
    )
    index = SourceLedgerIndex.build(ledger)
    assert [block.kind for block in index.blocks] == ["plugin", "include", "entry"]
    assert len(index.entry_blocks) == 1
    assert index.plugin_names == {"beancount.plugins.implicit_prices"}


def test_invalid_utf8_is_a_workspace_error(ledger):
    ledger.write_bytes(b"; invalid UTF-8: " + bytes([255]) + b"\n")
    with pytest.raises(LedgerWorkspaceError, match="invalid UTF-8"):
        LedgerWorkspace.capture(ledger)


def test_builder_rejects_workspace_temp_storage_before_creation(ledger, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(ledger.parent))
    with pytest.raises(SnapshotBuildError, match="temporary storage must be outside"):
        build(ledger)
    assert sorted(path.name for path in ledger.parent.iterdir()) == [
        "accounts.bean",
        "main.bean",
    ]


def test_exact_source_prices_and_supported_plugins_are_preserved(ledger):
    ledger.write_bytes(
        b'option "operating_currency" "USD"\n'
        b'plugin "beancount.plugins.implicit_prices"\n'
        b"2024-01-01 open Assets:Cash GBP\n"
        b"2024-01-01 open Equity:Opening-Balances USD\n\n"
        b'2024-01-02 * "Total price"\n'
        b"  Assets:Cash -1000 GBP @@ 1234.56 USD\n"
        b"  Equity:Opening-Balances 1234.56 USD\n\n"
        b'2024-01-03 * "Unit price"\n'
        b"  Assets:Cash -2 GBP @ 1.2500 USD\n"
        b"  Equity:Opening-Balances 2.5000 USD\n"
    )
    content, stats = build(ledger)
    assert "-1000 GBP @@ 1234.56 USD" in content
    assert "-2 GBP @ 1.2500 USD" in content
    assert 'plugin "' not in content
    entries, errors, _ = parser.parse_string(content)
    assert not errors
    assert any(isinstance(entry, data.Price) for entry in entries)
    assert stats.generated >= 1


def test_escaped_include_decoy_cannot_hide_loader_escape(ledger, tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "outside.bean").write_bytes(b"2024-01-01 open Assets:Secret USD\n")
    decoy = ledger.parent / r"\.\." / "outside"
    decoy.mkdir(parents=True)
    (decoy / "outside.bean").write_bytes(b"2024-01-01 open Assets:Decoy USD\n")
    ledger.write_text(
        'include "' + r"\.\./outside/outside.bean" + '"\n', encoding="utf-8"
    )
    _, errors, options = parser.parse_file(str(ledger))
    assert not errors
    assert options["include"] == ["../outside/outside.bean"]

    def forbidden_loader(*args, **kwargs):
        pytest.fail("Unsafe source reached the recursive loader")

    monkeypatch.setattr(snapshot_builder.loader, "load_file", forbidden_loader)
    with pytest.raises(SnapshotBuildError, match="Unsafe source include path"):
        build(ledger)


def test_beancount_suffix_supported_for_included_sources(ledger):
    included = ledger.parent / "accounts.beancount"
    (ledger.parent / "accounts.bean").rename(included)
    ledger.write_bytes(b'include "*.beancount"\n')
    assert (
        LedgerWorkspace.capture(ledger).include_counts[
            included.relative_to(ledger.parent)
        ]
        == 1
    )
    assert "2024-01-01 open Assets:Cash USD" in build(ledger)[0]


def test_private_copy_override_is_read_by_full_loader_and_cleaned(ledger):
    original = (ledger.parent / "accounts.bean").read_bytes()
    replacement = b"2024-01-01 open Assets:Candidate USD\n"
    with LedgerWorkspace.capture(ledger).private_copy(
        {Path("accounts.bean"): replacement}
    ) as private_root:
        assert private_root != ledger
        assert not private_root.is_relative_to(ledger.parent)
        assert private_root.read_bytes() == ledger.read_bytes()
        assert private_root.stat().st_mode & 0o777 == 0o600
        assert private_root.parent.stat().st_mode & 0o777 == 0o700
        entries, errors, _ = loader.load_file(str(private_root))
        assert not errors
        assert [entry.account for entry in entries if isinstance(entry, data.Open)] == [
            "Assets:Candidate"
        ]
        assert (ledger.parent / "accounts.bean").read_bytes() == original
    assert not private_root.exists()


def test_private_copy_cleanup_on_loader_exception(ledger):
    with (
        pytest.raises(RuntimeError, match="synthetic failure"),
        LedgerWorkspace.capture(ledger).private_copy() as private_root,
    ):
        raise RuntimeError("synthetic failure")
    assert not private_root.exists()


def test_reusable_capture_can_stage_independent_candidates(ledger):
    captured = LedgerWorkspace.capture(ledger)
    for account in ("First", "Second"):
        with captured.private_copy(
            {Path("accounts.bean"): f"2024-01-01 open Assets:{account} USD\n".encode()}
        ) as root:
            entries, errors, _ = loader.load_file(str(root))
            assert not errors
            assert [
                entry.account for entry in entries if isinstance(entry, data.Open)
            ] == [f"Assets:{account}"]
    assert captured.files[Path("accounts.bean")] == b"2024-01-01 open Assets:Cash USD\n"


def test_override_glob_cannot_silently_omit_unsupported_sources(ledger):
    (ledger.parent / "secret.txt").write_bytes(b"2024-01-01 open Assets:Omitted USD\n")
    captured = LedgerWorkspace.capture(ledger)
    with (
        pytest.raises(LedgerWorkspaceError, match="regular file"),
        captured.private_copy({Path("main.bean"): b'include "*"\n'}),
    ):
        pytest.fail("Incomplete candidate was materialized")


@pytest.mark.parametrize(
    "source",
    [
        b'option "documents" "docs"\n',
        b'option "documents" "../outside"\n',
        b'option "insert_pythonpath" "TRUE"\n',
        b'2024-01-01 document Assets:Cash "receipt.pdf"\n',
    ],
)
def test_auxiliary_dependent_sources_fail_closed(ledger, source):
    ledger.write_bytes(source)
    with (
        pytest.raises(LedgerWorkspaceError, match="source-only ledger copy"),
        LedgerWorkspace.capture(ledger).private_copy(),
    ):
        pytest.fail("Auxiliary-dependent ledger was silently changed")


@pytest.mark.parametrize("name", ["$ROOT.bean", "ledger.gpg", "ledger.asc"])
def test_root_names_with_loader_expansion_or_decryption_rejected(ledger, name):
    root = ledger.rename(ledger.with_name(name))
    with pytest.raises(LedgerWorkspaceError, match="root filenames are unsupported"):
        LedgerWorkspace.capture(root)


def test_parent_components_in_configured_root_rejected_before_normalization(ledger):
    root = ledger.parent / "unused" / ".." / ledger.name
    with pytest.raises(SnapshotBuildError, match="Unsafe ledger root path"):
        build(root)


def test_private_copy_rejects_symlink_temp_storage(ledger, tmp_path, monkeypatch):
    alias = tmp_path / "temp-alias"
    alias.symlink_to(ledger.parent, target_is_directory=True)
    monkeypatch.setenv("TMPDIR", str(alias))
    with (
        pytest.raises(LedgerWorkspaceError, match="Symlink source include"),
        LedgerWorkspace.capture(ledger).private_copy(),
    ):
        pytest.fail("Created temporary source files in the ledger")
    assert sorted(path.name for path in ledger.parent.iterdir()) == [
        "accounts.bean",
        "main.bean",
    ]


def test_private_copy_temp_parent_is_pinned_during_full_loading(
    ledger, tmp_path, monkeypatch
):
    temporary_root = tmp_path / "temporary"
    temporary_root.mkdir()
    moved = tmp_path / "moved"
    monkeypatch.setenv("TMPDIR", str(temporary_root))
    with LedgerWorkspace.capture(ledger).private_copy() as private_root:
        temporary_root.rename(moved)
        temporary_root.symlink_to(ledger.parent, target_is_directory=True)
        entries, errors, _ = loader.load_file(str(private_root))
        assert not errors
        assert [entry.account for entry in entries if isinstance(entry, data.Open)] == [
            "Assets:Cash"
        ]
    assert list(moved.iterdir()) == []
    assert sorted(path.name for path in ledger.parent.iterdir()) == [
        "accounts.bean",
        "main.bean",
    ]


def test_read_workspace_file_is_exact_and_does_not_parse(ledger):
    content = b"; exact source\r\nnot necessarily valid standalone\n"
    (ledger.parent / "accounts.bean").write_bytes(content)
    assert read_workspace_file(ledger.parent, Path("accounts.bean")) == content
    with pytest.raises(FileNotFoundError):
        read_workspace_file(ledger.parent, Path("missing.bean"))


@pytest.mark.parametrize(
    "relative",
    [
        "../outside.bean",
        "/outside.bean",
        ".hidden/a.bean",
        ".hidden.bean",
        "secret.txt",
        ".",
        "",
        "C:/outside.bean",
        "bad\x00.bean",
    ],
)
def test_read_workspace_rejects_unsafe_or_unsupported_paths(ledger, relative):
    with pytest.raises(LedgerWorkspaceError):
        read_workspace_file(ledger.parent, Path(relative))


@pytest.mark.parametrize("parent_link", [False, True])
def test_read_workspace_rejects_symlink_components(ledger, tmp_path, parent_link):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "stolen.bean").write_bytes(b"secret")
    link = ledger.parent / "link.bean"
    link.symlink_to(outside if parent_link else outside / "stolen.bean")
    relative = Path("link.bean/stolen.bean") if parent_link else Path("link.bean")
    with pytest.raises(LedgerWorkspaceError, match="Symlink source include"):
        read_workspace_file(ledger.parent, relative)


def test_glob_workspace_matches_are_sorted_bounded_and_nonhidden(ledger, tmp_path):
    nested = ledger.parent / "nested"
    nested.mkdir()
    (nested / "price.beancount").write_bytes(b"2024-01-01 price EUR 1.10 USD\n")
    (nested / ".hidden.bean").write_bytes(b"secret")
    (nested / "secret.txt").write_bytes(b"secret")
    (nested / "empty.bean").mkdir()
    hidden = ledger.parent / ".hidden"
    hidden.mkdir()
    (hidden / "secret.bean").write_bytes(b"secret")
    (nested / "outside").symlink_to(tmp_path, target_is_directory=True)
    (nested / "link.bean").symlink_to(ledger)
    files = glob_workspace_files(ledger.parent, "**/*")
    assert list(files) == [
        Path("accounts.bean"),
        Path("main.bean"),
        Path("nested/price.beancount"),
    ]
    assert (
        files[Path("accounts.bean")] == (ledger.parent / "accounts.bean").read_bytes()
    )
    assert glob_workspace_files(ledger.parent, "missing/*.bean") == {}


@pytest.mark.parametrize(
    "pattern", ["../*.bean", "/tmp/*.bean", ".hidden/*.bean", ".", ""]
)
def test_glob_workspace_rejects_unsafe_patterns(ledger, pattern):
    with pytest.raises(LedgerWorkspaceError):
        glob_workspace_files(ledger.parent, pattern)


@pytest.mark.parametrize("operation", ["read", "glob"])
def test_public_helpers_reject_file_swapped_to_symlink(
    ledger, tmp_path, monkeypatch, operation
):
    outside = tmp_path / "secret.bean"
    outside.write_bytes(b"secret")
    original_open = workspace_module.os.open

    def swapping_open(path, flags, *args, **kwargs):
        if path == "accounts.bean":
            (ledger.parent / "accounts.bean").unlink()
            (ledger.parent / "accounts.bean").symlink_to(outside)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(workspace_module.os, "open", swapping_open)
    with pytest.raises(LedgerWorkspaceError, match="Symlink source include"):
        if operation == "read":
            read_workspace_file(ledger.parent, Path("accounts.bean"))
        else:
            glob_workspace_files(ledger.parent, "*.bean")


def test_per_file_budget_applies_to_capture_reads_globs_and_overrides(
    ledger, monkeypatch
):
    captured = LedgerWorkspace.capture(ledger)
    monkeypatch.setattr(workspace_module, "MAX_FILE_BYTES", 8)
    for operation in (
        lambda: LedgerWorkspace.capture(ledger),
        lambda: read_workspace_file(ledger.parent, Path("main.bean")),
        lambda: glob_workspace_files(ledger.parent, "*.bean"),
        lambda: captured.materialize(
            ledger.parent.parent / "private", {Path("accounts.bean"): b"x" * 9}
        ),
    ):
        with pytest.raises(LedgerWorkspaceError, match="per-file byte budget"):
            operation()


@pytest.mark.parametrize(
    "constant",
    [
        "MAX_SCAN_ENTRIES",
        "MAX_GLOB_STEPS",
        "MAX_WORKSPACE_FILES",
        "MAX_WORKSPACE_BYTES",
    ],
)
def test_public_glob_budgets(ledger, monkeypatch, constant):
    monkeypatch.setattr(workspace_module, constant, 0)
    with pytest.raises(LedgerWorkspaceError, match="budget exceeded"):
        glob_workspace_files(ledger.parent, "*.bean")


def test_repeated_include_exponential_dag_stops_before_recursive_loader(
    ledger, monkeypatch
):
    for level in range(12):
        (ledger.parent / f"level{level}.bean").write_text(
            f'include "level{level + 1}.bean"\n' * 2 if level < 11 else "; leaf\n",
            encoding="utf-8",
        )
    ledger.write_bytes(b'include "level0.bean"\n')
    monkeypatch.setattr(workspace_module, "MAX_INCLUDE_VISITS", 64)
    with pytest.raises(SnapshotBuildError, match="visit budget exceeded"):
        build(ledger)


def test_plugin_ledger_fixture_retains_source_roundtrip_after_private_copy():
    fixture = Path(__file__).parent / "fixtures" / "plugin_ledger" / "main.bean"
    content, stats = build(fixture)
    assert "@@" in content
    assert 'include "' not in content
    assert 'plugin "' not in content
    _, errors, _ = loader.load_string(content)
    assert not errors
    assert stats.retained > 0
    assert stats.generated > 0


def test_direct_workspace_read_rejects_special_files_without_blocking(ledger):
    fifo = ledger.parent / "fifo.bean"
    workspace_module.os.mkfifo(fifo)
    with pytest.raises(LedgerWorkspaceError, match="regular file"):
        read_workspace_file(ledger.parent, Path("fifo.bean"))


@pytest.mark.parametrize("operation", ["read", "glob"])
def test_public_helpers_reject_directory_swapped_to_symlink(
    ledger, tmp_path, monkeypatch, operation
):
    nested = ledger.parent / "nested"
    nested.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.bean").write_bytes(b"secret")
    original_open = workspace_module.os.open

    def swapping_open(path, flags, *args, **kwargs):
        if path == "nested":
            nested.rmdir()
            nested.symlink_to(outside, target_is_directory=True)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(workspace_module.os, "open", swapping_open)
    with pytest.raises(LedgerWorkspaceError, match="Symlink source include"):
        if operation == "read":
            read_workspace_file(ledger.parent, Path("nested/secret.bean"))
        else:
            glob_workspace_files(ledger.parent, "nested/*.bean")


def test_growth_during_read_cannot_exceed_per_file_budget(ledger, monkeypatch):
    source = ledger.parent / "accounts.bean"
    monkeypatch.setattr(workspace_module, "MAX_FILE_BYTES", source.stat().st_size + 2)
    original_read = workspace_module.os.read
    changed = False

    def growing_read(descriptor, count):
        nonlocal changed
        if not changed:
            changed = True
            source.write_bytes(source.read_bytes() + b"x" * 32)
        return original_read(descriptor, count)

    monkeypatch.setattr(workspace_module.os, "read", growing_read)
    with pytest.raises(LedgerWorkspaceError, match="per-file byte budget"):
        read_workspace_file(ledger.parent, Path("accounts.bean"))


def test_include_depth_budget_rejects_deep_graph_before_loader(ledger, monkeypatch):
    for level in range(5):
        (ledger.parent / f"level{level}.bean").write_text(
            f'include "level{level + 1}.bean"\n' if level < 4 else "; leaf\n",
            encoding="utf-8",
        )
    ledger.write_bytes(b'include "level0.bean"\n')
    monkeypatch.setattr(workspace_module, "MAX_INCLUDE_DEPTH", 3)
    with pytest.raises(SnapshotBuildError, match="include depth budget"):
        build(ledger)
