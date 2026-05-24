'''
Test infrastructure endpoints
'''

import os
import pytest
import main
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


class TestInfrastructureConfig:
    """Tests for /infrastructure endpoint with config.bean"""

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
