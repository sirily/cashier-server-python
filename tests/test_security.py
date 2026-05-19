"""Security and container-readiness endpoint tests."""

import os
import subprocess

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


def test_infrastructure_rejects_control_characters(configured_test_book):
    response = client.get("/infrastructure?file_path=config.bean%00.py")

    assert response.status_code == 400


def test_infrastructure_rejects_directory_requests(configured_test_book):
    response = client.get("/infrastructure", params={"file_path": "."})

    assert response.status_code == 404


def test_infrastructure_glob_rejects_parent_directory_traversal(configured_test_book):
    response = client.get("/infrastructure", params={"file_path": "../*.bean"})

    assert response.status_code == 403


def test_infrastructure_glob_rejects_absolute_paths(configured_test_book):
    response = client.get("/infrastructure", params={"file_path": "/tmp/*.bean"})

    assert response.status_code == 403


def test_infrastructure_glob_rejects_control_characters(configured_test_book):
    response = client.get("/infrastructure?file_path=prices%2F*.bean%00")

    assert response.status_code == 400


def test_infrastructure_glob_ignores_symlinks(configured_test_book, tmp_path, monkeypatch):
    root = tmp_path / "ledger"
    root.mkdir()
    (root / "book.bean").write_text("", encoding="utf-8")
    (root / "inside.bean").write_text("inside", encoding="utf-8")
    (tmp_path / "outside.bean").write_text("outside", encoding="utf-8")
    (root / "inside-link.bean").symlink_to(root / "inside.bean")
    (root / "outside-link.bean").symlink_to(tmp_path / "outside.bean")
    monkeypatch.setattr(main, "BEAN_FILE", str(root / "book.bean"))

    response = client.get("/infrastructure", params={"file_path": "*.bean"})

    assert response.status_code == 200
    assert response.json() == {
        "files": [
            {"path": "book.bean", "content": ""},
            {"path": "inside.bean", "content": "inside"},
        ]
    }


def test_infrastructure_rejects_encoded_traversal_and_does_not_exfiltrate(configured_test_book):
    """Encoded ../ must not read files outside the ledger workspace."""
    outside_path = os.path.abspath(os.path.join(TEST_DIR, "..", "pyproject.toml"))
    outside_content = open(outside_path, encoding="utf-8").read()

    response = client.get("/infrastructure?file_path=..%2Fpyproject.toml")

    assert response.status_code == 403
    assert outside_content not in response.text


def test_infrastructure_glob_cannot_exfiltrate_parent_files(configured_test_book):
    """Glob patterns with traversal must not enumerate/read parent directories."""
    outside_path = os.path.abspath(os.path.join(TEST_DIR, "..", "pyproject.toml"))
    outside_content = open(outside_path, encoding="utf-8").read()

    response = client.get("/infrastructure?file_path=..%2F*.toml")

    assert response.status_code == 403
    assert outside_content not in response.text


def test_infrastructure_symlink_to_outside_does_not_exfiltrate(configured_test_book, tmp_path, monkeypatch):
    """A symlink in the workspace must not expose target content outside it."""
    root = tmp_path / "ledger"
    root.mkdir()
    (root / "book.bean").write_text("", encoding="utf-8")
    outside_secret = tmp_path / "secret.bean"
    outside_secret.write_text("SECRET_OUTSIDE_WORKSPACE", encoding="utf-8")
    (root / "leaked.bean").symlink_to(outside_secret)
    monkeypatch.setattr(main, "BEAN_FILE", str(root / "book.bean"))

    literal_response = client.get("/infrastructure", params={"file_path": "leaked.bean"})
    glob_response = client.get("/infrastructure", params={"file_path": "*.bean"})

    assert literal_response.status_code == 403
    assert "SECRET_OUTSIDE_WORKSPACE" not in literal_response.text
    assert glob_response.status_code == 200
    assert "SECRET_OUTSIDE_WORKSPACE" not in glob_response.text
    assert [item["path"] for item in glob_response.json()["files"]] == ["book.bean"]


def test_infrastructure_file_path_is_not_executed_as_shell(configured_test_book, tmp_path):
    """Shell metacharacters in file_path must be inert path text, not commands."""
    marker = tmp_path / "rce-marker"
    payload = f"config.bean; touch {marker}"

    response = client.get("/infrastructure", params={"file_path": payload})

    assert response.status_code == 404
    assert not marker.exists()


def test_infrastructure_glob_file_path_is_not_executed_as_shell(configured_test_book, tmp_path):
    """Glob file_path is expanded by pathlib only and must not execute shell syntax."""
    marker = tmp_path / "rce-marker-glob"
    payload = f"prices/*.bean; touch {marker}"

    response = client.get("/infrastructure", params={"file_path": payload})

    assert response.status_code == 404
    assert not marker.exists()


def test_infrastructure_does_not_call_subprocess_for_file_path(configured_test_book, monkeypatch):
    """The infrastructure endpoint must not dispatch file_path through subprocess."""

    def fail_subprocess(*args, **kwargs):
        raise AssertionError("subprocess must not be called by /infrastructure")

    monkeypatch.setattr(subprocess, "run", fail_subprocess)

    literal_response = client.get("/infrastructure", params={"file_path": "config.bean"})
    glob_response = client.get("/infrastructure", params={"file_path": "prices/*.bean"})

    assert literal_response.status_code == 200
    assert glob_response.status_code == 200


def test_shutdown_is_disabled_by_default(configured_test_book):
    response = client.get("/shutdown")

    assert response.status_code == 403
