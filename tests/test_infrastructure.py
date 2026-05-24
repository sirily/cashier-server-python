'''
Test infrastructure endpoints
'''

import os
import pytest
import main
from beancount import loader
from beancount.core import data
from beancount.parser import parser
from fastapi.testclient import TestClient


client = TestClient(main.app)


# Get the directory of the test files
TEST_DIR = os.path.dirname(os.path.abspath(__file__))


@pytest.fixture(autouse=True)
def set_bean_file():
    """Set BEANCOUNT_FILE to use the test book.bean file."""
    # Set the environment variable to the test book.bean
    test_bean_file = os.path.join(TEST_DIR, "book.bean")
    os.environ["BEANCOUNT_FILE"] = test_bean_file
    # Reload the main module's BEAN_FILE
    main.BEAN_FILE = test_bean_file
    yield
    # Cleanup (optional)


class TestInfrastructureRoot:
    """Tests for /infrastructure root book materialization."""

    def test_infrastructure_root_returns_materialized_content(self):
        """Root book response keeps {content} shape but applies Python plugins server-side."""
        test_bean_file = os.path.join(TEST_DIR, "materialized_root.bean")
        main.BEAN_FILE = test_bean_file

        response = client.get("/infrastructure", params={"file_path": "materialized_root.bean"})

        assert response.status_code == 200
        result = response.json()
        assert set(result.keys()) == {"content"}
        assert 'plugin "beancount.plugins.implicit_prices"' not in result["content"]
        assert 'include "' not in result["content"]
        assert 'Assets:Cash               10 USD' in result["content"]
        assert 'Equity:Opening-Balances  -10 USD' in result["content"]

    def test_infrastructure_root_materialization_reports_loader_errors(self):
        """Invalid root materialization returns a controlled error instead of raw source."""
        test_bean_file = os.path.join(TEST_DIR, "book.bean")
        main.BEAN_FILE = test_bean_file

        response = client.get("/infrastructure", params={"file_path": "book.bean"})

        assert response.status_code == 422
        result = response.json()
        assert result["detail"]["message"] == "Beancount root book could not be materialized"
        assert result["detail"]["errors"]

    def test_materialized_printer_preserves_all_entries_with_programmatic_metadata(self):
        """Programmatic metadata must not make materialized export drop entries.

        Lazy-beancount plugins can create parser-valid entries whose metadata
        contains plain Python values that Beancount's strict convenience printer
        refuses. The materialized export must still serialize every entry.
        """
        test_bean_file = os.path.join(TEST_DIR, "materialized_custom_root.bean")
        entries, errors, options_map = loader.load_file(test_bean_file)
        assert not errors
        entries = [entry._replace(meta={**entry.meta, "generated_index": 2}) for entry in entries]

        content = main.print_materialized_entries(entries, options_map)
        reparsed_entries, reparsed_errors, _ = parser.parse_string(content)

        assert not reparsed_errors
        assert len(reparsed_entries) == len(entries)
        assert '2024-01-01 custom "valuation" "Assets:Broker:Total" 7500.0 USD' in content
        assert "generated_index: 2" in content

    def test_infrastructure_root_preserves_custom_amount_entry(self):
        """Root materialization keeps Custom entries and Amount values importable."""
        test_bean_file = os.path.join(TEST_DIR, "materialized_custom_root.bean")
        main.BEAN_FILE = test_bean_file

        response = client.get("/infrastructure", params={"file_path": "materialized_custom_root.bean"})

        assert response.status_code == 200
        content = response.json()["content"]
        reparsed_entries, reparsed_errors, _ = parser.parse_string(content)
        assert not reparsed_errors
        assert any(type(entry).__name__ == "Custom" for entry in reparsed_entries)
        assert '2024-01-01 custom "valuation" "Assets:Broker:Total" 7500.0 USD' in content

    def test_canonicalization_preserves_programmatic_private_metadata_as_textual_metadata(self):
        """Programmatic metadata keys invalid in textual Beancount are renamed, not dropped."""
        test_bean_file = os.path.join(TEST_DIR, "materialized_custom_root.bean")
        entries, errors, options_map = loader.load_file(test_bean_file)
        assert not errors
        entries = [entry._replace(meta={**entry.meta, "_timesApplied": 2}) for entry in entries]

        canonical_entries = main.canonicalize_materialized_entries_for_pwa(entries)
        content = main.print_materialized_entries(canonical_entries, options_map)
        reparsed_entries, reparsed_errors, _ = parser.parse_string(content)

        assert not reparsed_errors
        assert len(reparsed_entries) == len(entries)
        assert not any(line.strip().startswith("_timesApplied:") for line in content.splitlines())
        assert "pwa_timesApplied: 2" in content
        assert any(entry.meta.get("pwa_timesApplied") == 2 for entry in reparsed_entries)
        assert '2024-01-01 custom "valuation" "Assets:Broker:Total" 7500.0 USD' in content

    def test_canonicalization_makes_all_programmatic_metadata_keys_parseable(self):
        """Invalid Python-only metadata key names become parseable PWA export names."""
        test_bean_file = os.path.join(TEST_DIR, "materialized_custom_root.bean")
        entries, errors, options_map = loader.load_file(test_bean_file)
        assert not errors
        entry = entries[0]._replace(
            meta={
                **entries[0].meta,
                "valid_key": "kept",
                "_timesApplied": 2,
                "_bad-key!": 3,
                "Foo": 4,
                "1foo": 5,
                "foo.bar": 6,
                "foo/bar": 7,
                "foo bar": 8,
                "éfoo": 9,
            }
        )

        canonical_entries = main.canonicalize_materialized_entries_for_pwa([entry])
        content = main.print_materialized_entries(canonical_entries, options_map)
        reparsed_entries, reparsed_errors, _ = parser.parse_string(content)

        assert not reparsed_errors
        assert reparsed_entries[0].meta["valid_key"] == "kept"
        expected_metadata = {
            "pwa_timesApplied": 2,
            "pwa_bad-key_": 3,
            "pwa_Foo": 4,
            "pwa_foo": 5,
            "pwa_foo_bar": 6,
            "pwa_foo_bar_2": 7,
            "pwa_foo_bar_3": 8,
            "pwa_foo_2": 9,
        }
        for key, value in expected_metadata.items():
            assert f"{key}:" in content
            assert reparsed_entries[0].meta[key] == value
        for invalid_key in ("_timesApplied", "_bad-key!", "Foo", "1foo", "foo.bar", "foo/bar", "foo bar", "éfoo"):
            assert not any(line.strip().startswith(f"{invalid_key}:") for line in content.splitlines())

    def test_canonicalization_preserves_colliding_metadata_values(self):
        """Renamed metadata never overwrites an existing valid key on the same entry."""
        test_bean_file = os.path.join(TEST_DIR, "materialized_custom_root.bean")
        entries, errors, options_map = loader.load_file(test_bean_file)
        assert not errors
        entry = entries[0]._replace(meta={**entries[0].meta, "pwa_foo": "original", "_foo": "renamed"})

        canonical_entries = main.canonicalize_materialized_entries_for_pwa([entry])
        content = main.print_materialized_entries(canonical_entries, options_map)
        reparsed_entries, reparsed_errors, _ = parser.parse_string(content)

        assert not reparsed_errors
        assert reparsed_entries[0].meta["pwa_foo"] == "original"
        assert reparsed_entries[0].meta["pwa_foo_2"] == "renamed"

    def test_canonicalization_covers_posting_metadata(self):
        """Posting metadata gets the same parseable-key treatment as entry metadata."""
        entries, errors, options_map = parser.parse_string(
            '''2024-01-01 * "Posting metadata"\n  Assets:Cash  1 USD\n  Equity:Opening-Balances -1 USD\n'''
        )
        assert not errors
        posting = entries[0].postings[0]._replace(meta={"pwa_post": "original", "_post": "renamed", "foo.bar": 3})
        entry = entries[0]._replace(postings=[posting, entries[0].postings[1]])

        canonical_entries = main.canonicalize_materialized_entries_for_pwa([entry])
        content = main.print_materialized_entries(canonical_entries, options_map)
        reparsed_entries, reparsed_errors, _ = parser.parse_string(content)

        assert not reparsed_errors
        reparsed_posting_meta = reparsed_entries[0].postings[0].meta
        assert reparsed_posting_meta["pwa_post"] == "original"
        assert reparsed_posting_meta["pwa_post_2"] == "renamed"
        assert reparsed_posting_meta["pwa_foo_bar"] == 3

    def test_canonicalization_removes_only_consumed_pad_directive(self):
        """A consumed Pad instruction is not exported as an active second-pass operation.

        This fixture represents the post-plugin state: both the operational Pad
        directive and its concrete inserted transaction are present. Removing the
        Pad is safe only if the transaction, its postings, and the balance check
        remain in the PWA snapshot.
        """
        fixture_path = os.path.join(TEST_DIR, "materialized_pad_snapshot.bean")
        entries, errors, options_map = parser.parse_file(fixture_path)
        assert not errors
        assert sum(isinstance(entry, data.Pad) for entry in entries) == 1
        expected_transactions = [entry for entry in entries if isinstance(entry, data.Transaction)]
        expected_balances = [entry for entry in entries if isinstance(entry, data.Balance)]

        canonical_entries = main.canonicalize_materialized_entries_for_pwa(entries)
        content = main.print_materialized_entries(canonical_entries, options_map)
        reparsed_entries, reparsed_errors, _ = parser.parse_string(content)

        assert not reparsed_errors
        assert not any(isinstance(entry, data.Pad) for entry in canonical_entries)
        assert not any(isinstance(entry, data.Pad) for entry in reparsed_entries)
        actual_transactions = [entry for entry in reparsed_entries if isinstance(entry, data.Transaction)]
        actual_balances = [entry for entry in reparsed_entries if isinstance(entry, data.Balance)]
        assert len(actual_transactions) == len(expected_transactions) == 1
        assert len(actual_balances) == len(expected_balances) == 1
        assert [(posting.account, posting.units) for posting in actual_transactions[0].postings] == [
            (posting.account, posting.units) for posting in expected_transactions[0].postings
        ]
        assert (actual_balances[0].account, actual_balances[0].amount) == (
            expected_balances[0].account,
            expected_balances[0].amount,
        )

    def test_canonicalization_keeps_every_non_pad_entry(self):
        """Canonicalization may consume Pad instructions, not unrelated entries."""
        fixture_path = os.path.join(TEST_DIR, "materialized_pad_snapshot.bean")
        entries, errors, _ = parser.parse_file(fixture_path)
        assert not errors

        canonical_entries = main.canonicalize_materialized_entries_for_pwa(entries)

        assert [entry for entry in canonical_entries] == [
            entry for entry in entries if not isinstance(entry, data.Pad)
        ]
        assert len(entries) - len(canonical_entries) == sum(isinstance(entry, data.Pad) for entry in entries)


class TestInfrastructure:


    def test_infrastructure_config_returns_config_file(self):
        """Test that infrastructure endpoint returns the config.bean file content."""
        response = client.get("/infrastructure", params={"file_path": "config.bean"})
        assert response.status_code == 200
        result = response.json()
        assert "content" in result
        
        # Read expected content from tests/config.bean
        config_path = os.path.join(TEST_DIR, "config.bean")
        with open(config_path, "r", encoding="utf-8") as f:
            expected_content = f.read()
        
        assert result["content"] == expected_content

    def test_infrastructure_config_returns_required_files(self):
        """Test that infrastructure endpoint returns the required config.bean file."""
        response = client.get("/infrastructure", params={"file_path": "config.bean"})
        assert response.status_code == 200
        result = response.json()
        assert "content" in result
        assert isinstance(result["content"], str)
        assert len(result["content"]) > 0


class TestInfrastructureAccounts:
    """Tests for /infrastructure endpoint with accounts.bean"""

    def test_infrastructure_accounts_returns_accounts_file(self):
        """Test that infrastructure endpoint returns the accounts.bean file content."""
        response = client.get("/infrastructure", params={"file_path": "accounts.bean"})
        assert response.status_code == 200
        result = response.json()
        assert "content" in result
        
        # Read expected content from tests/accounts.bean
        accounts_path = os.path.join(TEST_DIR, "accounts.bean")
        with open(accounts_path, "r", encoding="utf-8") as f:
            expected_content = f.read()
        
        assert result["content"] == expected_content

    def test_infrastructure_accounts_returns_required_files(self):
        """Test that infrastructure endpoint returns the required accounts.bean file."""
        response = client.get("/infrastructure", params={"file_path": "accounts.bean"})
        assert response.status_code == 200
        result = response.json()
        assert "content" in result
        assert isinstance(result["content"], str)


class TestInfrastructureCommodities:
    """Tests for /infrastructure endpoint with commodities.bean"""

    def test_infrastructure_commodities_returns_commodities_file(self):
        """Test that infrastructure endpoint returns the commodities.bean file content."""
        response = client.get("/infrastructure", params={"file_path": "commodities.bean"})
        assert response.status_code == 200
        result = response.json()
        assert "content" in result
        
        # Read expected content from tests/commodities.bean
        commodities_path = os.path.join(TEST_DIR, "commodities.bean")
        with open(commodities_path, "r", encoding="utf-8") as f:
            expected_content = f.read()
        
        assert result["content"] == expected_content

    def test_infrastructure_commodities_returns_required_files(self):
        """Test that infrastructure endpoint returns the required commodities.bean file."""
        response = client.get("/infrastructure", params={"file_path": "commodities.bean"})
        assert response.status_code == 200
        result = response.json()
        assert "content" in result
        assert isinstance(result["content"], str)


class TestInfrastructureGlob:
    """Tests for /infrastructure endpoint with glob patterns"""

    def test_glob_prices_bean_returns_all_files_sorted(self):
        """Test glob pattern prices/*.bean returns matching files in stable order."""
        response = client.get("/infrastructure", params={"file_path": "prices/*.bean"})
        assert response.status_code == 200
        result = response.json()
        assert result == {
            "files": [
                {"path": "prices/2023.bean", "content": "2023 content"},
                {"path": "prices/2024.bean", "content": "2024 content"},
            ]
        }

    def test_glob_only_returns_regular_files(self):
        """Test glob pattern ignores matching directories."""
        response = client.get("/infrastructure", params={"file_path": "prices/*"})
        assert response.status_code == 200
        result = response.json()
        assert [item["path"] for item in result["files"]] == [
            "prices/2023.bean",
            "prices/2024.bean",
        ]

    def test_glob_no_match_returns_404(self):
        """Test glob with no matches returns 404."""
        response = client.get("/infrastructure", params={"file_path": "prices/*.nonexistent"})
        assert response.status_code == 404
        assert "File not found" in response.json()["detail"]

    def test_glob_rejects_absolute_path(self):
        """Test glob rejects absolute paths."""
        response = client.get("/infrastructure", params={"file_path": "/etc/*.conf"})
        assert response.status_code == 403

    def test_glob_rejects_parent_traversal(self):
        """Test glob rejects parent traversal with double-dot."""
        response = client.get("/infrastructure", params={"file_path": "../config.bean"})
        assert response.status_code == 403
