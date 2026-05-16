"""Security and container-readiness endpoint tests."""

import os

from fastapi.testclient import TestClient

import main


client = TestClient(main.app)
TEST_DIR = os.path.dirname(os.path.abspath(__file__))


def setup_function():
    test_bean_file = os.path.join(TEST_DIR, "book.bean")
    os.environ["BEANCOUNT_FILE"] = test_bean_file
    main.BEAN_FILE = test_bean_file
    main.CASHIER_ENABLE_SHUTDOWN = False


def test_health_endpoint_reports_configured_bean_file():
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["beancount_file_configured"] is True


def test_infrastructure_rejects_parent_directory_traversal():
    response = client.get("/infrastructure", params={"file_path": "../pyproject.toml"})

    assert response.status_code == 403


def test_infrastructure_rejects_absolute_paths():
    response = client.get("/infrastructure", params={"file_path": "/etc/passwd"})

    assert response.status_code == 403


def test_shutdown_is_disabled_by_default():
    response = client.get("/shutdown")

    assert response.status_code == 403
