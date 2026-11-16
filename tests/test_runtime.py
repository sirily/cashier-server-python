"""Runtime configuration and Beancount-only request handling."""

import re
import subprocess
import sys

import pytest
import uvicorn
from fastapi.testclient import TestClient

import main


@pytest.mark.parametrize("path", ["/", "/reload"])
@pytest.mark.parametrize("bean_file", [None, ""])
def test_missing_beancount_configuration_never_starts_ledger(monkeypatch, path, bean_file):
    def unexpected_run(*args, **kwargs):
        pytest.fail("The server must never start Ledger CLI")

    monkeypatch.setattr(main, "BEAN_FILE", bean_file)
    monkeypatch.setattr(main, "CASHIER_API_TOKEN", "test-api-token")
    monkeypatch.setattr(subprocess, "run", unexpected_run)
    client = TestClient(main.app, headers={"Authorization": "Bearer test-api-token"})

    response = client.get(path, params={"query": "SELECT 1"})

    assert response.status_code == 500
    assert response.json() == {"detail": "BEANCOUNT_FILE must be configured"}


@pytest.mark.parametrize("bean_file", [None, ""])
def test_server_refuses_to_start_without_beancount_file(monkeypatch, bean_file):
    def unexpected_start(*args, **kwargs):
        pytest.fail("An unconfigured server must not load data or start Uvicorn")

    monkeypatch.setattr(main, "CASHIER_API_TOKEN", "test-api-token")
    monkeypatch.setattr(main, "BEAN_FILE", bean_file)
    monkeypatch.setattr(main, "preload_beancount_data", unexpected_start)
    monkeypatch.setattr(uvicorn, "Server", unexpected_start)

    with pytest.raises(RuntimeError, match="BEANCOUNT_FILE must be configured"):
        main.main()


@pytest.mark.parametrize("query", [None, ""])
@pytest.mark.asyncio
async def test_query_required(query):
    assert await main.index(query) == {"error": "No query provided"}


@pytest.mark.parametrize("key,cert,expected", [
    (None, None, (None, None)),
    ("key.pem", None, (None, None)),
    (None, "cert.pem", (None, None)),
    ("key.pem", "cert.pem", ("key.pem", "cert.pem")),
])
def test_server_passes_explicit_tls_options(monkeypatch, key, cert, expected):
    configs: list[uvicorn.Config] = []
    started: list[bool] = []
    connection = object()

    class Server:
        def __init__(self, config: uvicorn.Config):
            configs.append(config)

        def run(self):
            started.append(True)

    monkeypatch.setattr(main, "CASHIER_API_TOKEN", "test-api-token")
    monkeypatch.setattr(main, "BEAN_FILE", "test.bean")
    monkeypatch.setattr(main, "preload_beancount_data", lambda: connection)
    monkeypatch.setattr(main.app.state, "connection", None, raising=False)
    monkeypatch.setattr(main, "CASHIER_SSL_KEY", key)
    monkeypatch.setattr(main, "CASHIER_SSL_CERT", cert)
    monkeypatch.setattr(uvicorn, "Server", Server)

    main.main()

    assert started == [True]
    assert main.app.state.connection is connection
    assert len(configs) == 1
    assert (configs[0].ssl_keyfile, configs[0].ssl_certfile) == expected
    assert configs[0].host == "0.0.0.0"
    assert configs[0].port == 3000


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
    raise RuntimeError("reload failed")

private_value = "PRIVATE-LEDGER-CONTENT"
try:
    fail(private_value)
except RuntimeError:
    main.logger.exception("Refresh failure")
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
    assert "RuntimeError: reload failed" in stderr
    assert "Refresh failure" in stderr
    # The positive control proves this file-backed traceback detects the leak.
    assert ("PRIVATE-LEDGER-CONTENT" in stderr) is unsafe_diagnostics
