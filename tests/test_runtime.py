"""Runtime configuration and Beancount-only request handling."""

import re
import subprocess
import sys

import pytest
import uvicorn
from fastapi.testclient import TestClient

import main


@pytest.mark.parametrize("bean_file", [None, ""])
def test_missing_beancount_configuration_never_starts_ledger(monkeypatch, bean_file):
    def unexpected_run(*args, **kwargs):
        pytest.fail("The server must never start Ledger CLI")

    monkeypatch.setattr(main, "BEAN_FILE", bean_file)
    monkeypatch.setattr(main, "CASHIER_API_TOKEN", "test-api-token")
    monkeypatch.setattr(subprocess, "run", unexpected_run)
    client = TestClient(main.app, headers={"Authorization": "Bearer test-api-token"})

    response = client.get("/infrastructure", params={"file_path": "main.bean"})

    assert response.status_code == 500
    assert response.json() == {"detail": "BEANCOUNT_FILE environment variable not set"}


@pytest.mark.parametrize("bean_file", [None, ""])
def test_server_refuses_to_start_without_beancount_file(monkeypatch, bean_file):
    def unexpected_start(*args, **kwargs):
        pytest.fail("An unconfigured server must not start Uvicorn")

    monkeypatch.setattr(main, "CASHIER_API_TOKEN", "test-api-token")
    monkeypatch.setattr(main, "BEAN_FILE", bean_file)
    monkeypatch.setattr(uvicorn, "Server", unexpected_start)

    with pytest.raises(RuntimeError, match="BEANCOUNT_FILE must be configured"):
        main.main()


def test_server_starts_internal_http_listener_without_loading_ledger(monkeypatch):
    from beancount import loader

    def unexpected_load(*args, **kwargs):
        pytest.fail("Startup must not load a ledger")

    monkeypatch.setattr(loader, "load_file", unexpected_load)
    configs: list[uvicorn.Config] = []
    started: list[bool] = []

    class Server:
        def __init__(self, config: uvicorn.Config):
            configs.append(config)

        def run(self):
            started.append(True)

    monkeypatch.setattr(main, "CASHIER_API_TOKEN", "test-api-token")
    monkeypatch.setattr(main, "BEAN_FILE", "test.bean")
    monkeypatch.delattr(main.app.state, "connection", raising=False)

    monkeypatch.setattr(uvicorn, "Server", Server)

    main.main()

    assert started == [True]
    assert not hasattr(main.app.state, "connection")
    assert len(configs) == 1
    assert configs[0].is_ssl is False
    assert configs[0].limit_concurrency == 64
    assert configs[0].host == "0.0.0.0"
    assert configs[0].port == 3000


@pytest.mark.parametrize("configuration", [None, "", "missing", "invalid"])
def test_health_reports_configuration_without_reading_ledger(monkeypatch, tmp_path, configuration):
    from beancount import loader

    def unexpected_load(*args, **kwargs):
        pytest.fail("Health must not load or validate a ledger")

    bean_file = configuration
    if configuration in {"missing", "invalid"}:
        book = tmp_path / "main.bean"
        if configuration == "invalid":
            book.write_text("not a valid Beancount ledger", encoding="utf-8")
        bean_file = str(book)
    monkeypatch.setattr(main, "BEAN_FILE", bean_file)
    monkeypatch.setattr(main, "CASHIER_API_TOKEN", "")
    monkeypatch.setattr(loader, "load_file", unexpected_load)
    monkeypatch.setattr(main, "render_materialized_root_book", unexpected_load)

    response = TestClient(main.app).get("/health")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "beancount_file_configured": bool(bean_file)}


@pytest.mark.parametrize("unsafe_diagnostics", [False, True])
@pytest.mark.parametrize("colorize", [False, True])
def test_exception_logging_omits_private_local_values(tmp_path, monkeypatch, unsafe_diagnostics, colorize):
    monkeypatch.setenv("LOGURU_COLORIZE", str(colorize).lower())
    code = '''
import sys
import main

if len(sys.argv) > 2:
    main.logger.configure(handlers=[{"sink": sys.stderr, "diagnose": True}])

def fail(value):
    raise RuntimeError("operation failed")

private_value = "PRIVATE-LEDGER-CONTENT"
try:
    fail(private_value)
except RuntimeError:
    main.logger.exception("Operation failure")
'''
    script = tmp_path / "log_failure.py"
    script.write_text(code, encoding="utf-8")
    command = [
        sys.executable, "-c",
        "import runpy, sys; runpy.run_path(sys.argv[1], run_name='__main__')",
        str(script),
    ]
    if unsafe_diagnostics:
        command.append("unsafe")
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    assert ("\x1b[" in result.stderr) is colorize
    stderr = re.sub(r"\x1b\[[0-9;]*m", "", result.stderr)
    assert "RuntimeError: operation failed" in stderr
    assert "Operation failure" in stderr
    # The positive control proves this file-backed traceback detects the leak.
    assert ("PRIVATE-LEDGER-CONTENT" in stderr) is unsafe_diagnostics
