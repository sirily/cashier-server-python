"""Shared, opt-in fixtures for writeback tests."""

from collections.abc import Iterator
from pathlib import Path

import pytest

import main


@pytest.fixture
def configured_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[str]:
    """Configure a ledger whose root includes the single writable manual file."""
    base = Path(__file__).with_name("materialized_root.bean").read_text()

    main_bean = tmp_path / "main.bean"
    main_bean.write_text(base.strip() + '\ninclude "manual_transactions.bean"\n', encoding="utf-8")

    manual_file = tmp_path / "manual_transactions.bean"
    manual_file.write_text("")

    monkeypatch.setenv("BEANCOUNT_FILE", str(main_bean))
    monkeypatch.setattr(main, "BEAN_FILE", str(main_bean))
    monkeypatch.setenv("BEANCOUNT_MANUAL_TRANSACTIONS_FILE", str(manual_file))

    yield str(manual_file)
