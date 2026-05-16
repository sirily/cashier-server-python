"""Security and container-readiness endpoint tests."""

import os

import pytest
from fastapi.testclient import TestClient

import main


client = TestClient(main.app)
TEST_DIR = os.path.dirname(os.path.abspath(__file__))


@pytest.fixture
def configured_test_book(monkeypatch):
    """Point the module-level configuration at the test Beancount book."""
    test_bean_file = os.path.join(TEST_DIR, "book.bean")
    monkeypatch.setenv("BEANCOUNT_FILE", test_bean_file)
    monkeypatch.setattr(main, "BEAN_FILE", test_bean_file)
    monkeypatch.setattr(main, "CASHIER_ENABLE_SHUTDOWN", False)
    return test_bean_file


def test_health_endpoint_reports_configured_bean_file(configured_test_book):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["beancount_file_configured"] is True


def test_infrastructure_returns_file_inside_ledger_directory(configured_test_book):
    response = client.get("/infrastructure", params={"file_path": "config.bean"})

    assert response.status_code == 200
    assert response.json()["content"] == open(
        os.path.join(TEST_DIR, "config.bean"), encoding="utf-8"
    ).read()


def test_infrastructure_rejects_parent_directory_traversal(configured_test_book):
    response = client.get("/infrastructure", params={"file_path": "../pyproject.toml"})

    assert response.status_code == 403


def test_infrastructure_rejects_absolute_paths(configured_test_book):
    response = client.get("/infrastructure", params={"file_path": "/etc/passwd"})

    assert response.status_code == 403


def test_shutdown_is_disabled_by_default(configured_test_book):
    response = client.get("/shutdown")

    assert response.status_code == 403
