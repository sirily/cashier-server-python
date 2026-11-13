"""Runtime helpers must preserve responses without blocking the event loop."""


import subprocess
import sys
import threading
from pathlib import Path

import pytest
import uvicorn

import main


@pytest.mark.asyncio
@pytest.mark.parametrize("query", [None, ""])
async def test_ledger_without_query_does_not_run_subprocess(monkeypatch, query):
    def unexpected_run(*args, **kwargs):
        pytest.fail("Empty queries must not start Ledger")

    monkeypatch.setattr(subprocess, "run", unexpected_run)
    assert await main.ledger(query) == {"error": "No query provided"}


@pytest.mark.asyncio
async def test_ledger_runs_in_worker_and_returns_output_lines(monkeypatch):
    event_loop_thread = threading.get_ident()

    def run(command, **kwargs):
        assert threading.get_ident() != event_loop_thread
        assert command == ["ledger", "balance", "Assets"]
        assert kwargs == {
            "capture_output": True, "text": True, "check": True, "encoding": "utf-8",
        }
        return subprocess.CompletedProcess(command, 0, stdout="Assets:Cash 10 USD\nTotal 10 USD\n")

    monkeypatch.setattr(subprocess, "run", run)
    assert await main.ledger("balance Assets") == ["Assets:Cash 10 USD", "Total 10 USD"]


@pytest.mark.asyncio
async def test_ledger_preserves_command_error_response(monkeypatch):
    error = subprocess.CalledProcessError(1, ["ledger", "balance"], stderr="Invalid ledger")

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(subprocess, "run", fail)
    assert await main.ledger("balance") == {"error": str(error), "stderr": "Invalid ledger"}


@pytest.mark.asyncio
async def test_hello_reads_in_worker_and_returns_base64(monkeypatch):
    event_loop_thread = threading.get_ident()

    def read_bytes(path):
        assert threading.get_ident() != event_loop_thread
        assert path == Path("hello.png")
        return b"image bytes"

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    assert await main.hello_img() == "aW1hZ2UgYnl0ZXM="


@pytest.mark.parametrize("key,cert,expected", [
    (None, None, (None, None)),
    ("key.pem", None, (None, None)),
    (None, "cert.pem", (None, None)),
    ("key.pem", "cert.pem", ("key.pem", "cert.pem")),
])
def test_server_passes_explicit_tls_options(monkeypatch, key, cert, expected):
    configs: list[uvicorn.Config] = []
    started: list[bool] = []

    class Server:
        def __init__(self, config: uvicorn.Config):
            configs.append(config)

        def run(self):
            started.append(True)

    monkeypatch.setattr(main, "CASHIER_API_TOKEN", "test-api-token")
    monkeypatch.setattr(main, "BEAN_FILE", None)
    monkeypatch.setattr(main, "CASHIER_SSL_KEY", key)
    monkeypatch.setattr(main, "CASHIER_SSL_CERT", cert)
    monkeypatch.setattr(uvicorn, "Server", Server)
    monkeypatch.setattr(main.app.state, "server", None, raising=False)

    main.main()

    assert started == [True]
    assert len(configs) == 1
    assert (configs[0].ssl_keyfile, configs[0].ssl_certfile) == expected
    assert configs[0].host == "0.0.0.0"
    assert configs[0].port == 3000


@pytest.mark.parametrize("unsafe_diagnostics", [False, True])
def test_exception_logging_omits_private_local_values(tmp_path, unsafe_diagnostics):
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
    assert "RuntimeError: reload failed" in result.stderr
    assert "Refresh failure" in result.stderr
    # The positive control proves this file-backed traceback detects the leak.
    assert ("PRIVATE-LEDGER-CONTENT" in result.stderr) is unsafe_diagnostics
