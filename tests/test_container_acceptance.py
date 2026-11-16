"""Container-level acceptance tests for production Docker image.

Builds the clean Docker image from the repo and tests it against
a vendored plugin-bearing ledger fixture representative of the
Lazy Beancount workspace.  No runtime pip install / host venv
mount / separate QA image — the test uses only the PR-built image.
"""

import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import quote

import pytest

FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "plugin_ledger")
IMAGE_TAG = "cashier-auth-server:e2e"
CONTAINER_NAME = f"cashier-auth-acceptance-{os.getpid()}-{secrets.token_hex(4)}"
HOST_PORT = None
AUTH_CREDENTIAL = secrets.token_urlsafe(32)

_DOCKER_SKIP_REASON = "Set DOCKER_ACCEPTANCE=1 to run container acceptance tests"
pytestmark = pytest.mark.skipif(
    os.environ.get("DOCKER_ACCEPTANCE", "").lower() not in ("1", "true", "yes"),
    reason=_DOCKER_SKIP_REASON,
)


def _ensure_image():
    """Build image (always unless CASHIER_ACCEPTANCE_SKIP_BUILD is set)."""
    if os.environ.get("CASHIER_ACCEPTANCE_SKIP_BUILD", "").lower() in ("1", "true"):
        return
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    subprocess.run(
        ["docker", "build", "-t", IMAGE_TAG, "."],
        cwd=repo_root,
        check=True,
        capture_output=True,
    )


def _run_container():
    global HOST_PORT
    subprocess.run(
        [
            "docker", "run", "-d",
            "--name", CONTAINER_NAME,
            "-p", "127.0.0.1::3000",
            "-v", f"{FIXTURE_DIR}:/workspace:ro",
            "-e", "BEANCOUNT_FILE=/workspace/main.bean",
            "-e", "CASHIER_API_TOKEN",
            IMAGE_TAG,
        ],
        check=True,
        capture_output=True,
        env={**os.environ, "CASHIER_API_TOKEN": AUTH_CREDENTIAL},
    )
    binding = subprocess.run(
        ["docker", "port", CONTAINER_NAME, "3000/tcp"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert binding.startswith("127.0.0.1:"), binding
    HOST_PORT = int(binding.rsplit(":", 1)[1])


def _wait_for_health(timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = urllib.request.urlopen(f"http://127.0.0.1:{HOST_PORT}/health", timeout=5)
            if resp.status == 200:
                return True
        except (urllib.error.URLError, ConnectionError, OSError):
            time.sleep(2)
    return False


def _container_logs():
    try:
        return subprocess.run(
            ["docker", "logs", CONTAINER_NAME],
            check=True, capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _get(path, params=None, authorization: str | None = f"Bearer {AUTH_CREDENTIAL}", method="GET", data=None, host=None):
    url = f"http://127.0.0.1:{HOST_PORT}{path}"
    if params:
        qs = "&".join(f"{k}={quote(v)}" for k, v in params.items())
        url = f"{url}?{qs}"
    headers = {} if authorization is None else {"Authorization": authorization}
    if host is not None:
        headers["Host"] = host
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, headers=headers, method=method, data=data)
    try:
        resp = urllib.request.urlopen(request, timeout=10)
    except urllib.error.HTTPError as error:
        resp = error
    with resp:
        return resp.status, resp.read().decode("utf-8")


def _stop_container():
    subprocess.run(["docker", "stop", CONTAINER_NAME], check=False, capture_output=True, timeout=30)
    subprocess.run(["docker", "rm", "-f", CONTAINER_NAME], check=False, capture_output=True, timeout=30)


class TestContainerAcceptance:

    @classmethod
    def setup_class(cls):
        _stop_container()
        _ensure_image()
        _run_container()
        if not _wait_for_health(timeout=90):
            logs = _container_logs()
            _stop_container()
            msg = "Container did not become healthy"
            if logs:
                msg += f"\nContainer logs:\n{logs.stdout}\n{logs.stderr}"
            raise RuntimeError(msg)

    @classmethod
    def teardown_class(cls):
        _stop_container()

    def test_ping_succeeds(self):
        status, body = _get("/ping")
        assert status == 200
        assert body == '"pong"'

    @pytest.mark.parametrize("path", ["/hello", "/shutdown"])
    def test_removed_endpoints_return_404(self, path):
        status, body = _get(path)
        assert status == 404
        assert json.loads(body) == {"detail": "Not Found"}

    def test_missing_beancount_file_container_refuses_startup(self):
        name = f"{CONTAINER_NAME}-missing-beancount-file"
        try:
            result = subprocess.run(
                ["docker", "run", "--name", name, "--network", "none",
                 "-e", "BEANCOUNT_FILE=", "-e", "CASHIER_API_TOKEN", IMAGE_TAG],
                check=False, capture_output=True, text=True, timeout=30,
                env={**os.environ, "CASHIER_API_TOKEN": AUTH_CREDENTIAL},
            )
            assert result.returncode == 1, result.stdout + result.stderr
            assert "BEANCOUNT_FILE must be configured" in result.stderr
            assert "Uvicorn running" not in result.stdout + result.stderr
            assert AUTH_CREDENTIAL not in result.stdout + result.stderr
        finally:
            subprocess.run(["docker", "rm", "-f", name], check=False, capture_output=True, timeout=30)

    def test_health_ok(self):
        status, body = _get("/health", authorization=None)
        assert status == 200
        data = json.loads(body)
        assert data["ok"] is True
        assert data["beancount_file_configured"] is True
        assert data["beancount_loaded"] is True

    @pytest.mark.parametrize("authorization", [None, "Bearer wrong-token", "Basic wrong-token", "Bearer "])
    @pytest.mark.parametrize("path,method,data", [
        ("/ping", "GET", None),
        ("/infrastructure?file_path=main.bean", "GET", None),
        ("/xact", "POST", b'{"transactions": []}'),
        ("/reload", "GET", None),
    ])
    def test_invalid_auth_rejected(self, authorization, path, method, data):
        status, body = _get(path, authorization=authorization, method=method, data=data)
        assert status == 401
        assert json.loads(body) == {"detail": "Invalid or missing API token"}
        assert AUTH_CREDENTIAL not in body

    @pytest.mark.parametrize("host", ["testserver/health?", "testserver/health#"])
    @pytest.mark.parametrize("path,method,data", [
        ("/infrastructure?file_path=main.bean", "GET", None),
        ("/?query=SELECT%201", "GET", None),
        ("/xact", "POST", b'{"transactions": []}'),
    ])
    def test_malformed_host_never_bypasses_authentication(self, host, path, method, data):
        status, _ = _get(path, authorization=None, method=method, data=data, host=host)
        assert status == 400

    @pytest.mark.parametrize("query", [
        "CREATE TABLE accounts (account str)",
        "INSERT INTO accounts (account) VALUES ('Assets:Fake')",
        "CREATE TABLE external (line str) USING 'csv:/outside/synthetic.txt'",
    ])
    def test_bql_mutations_and_external_sources_rejected(self, query):
        status, _ = _get("/", params={"query": query})
        assert status == 400
        status, body = _get("/", params={"query": "SELECT account FROM #accounts LIMIT 1"})
        assert status == 200
        assert len(json.loads(body)) == 1

    def test_pwa_payees_query_remains_available(self):
        status, body = _get("/", params={
            "query": "SELECT DISTINCT COALESCE(payee, narration) AS payee FROM transactions ORDER BY payee LIMIT 10",
        })
        assert status == 200
        assert json.loads(body)

    def test_infrastructure_rejects_hidden_nonledger_paths(self):
        status, _ = _get("/infrastructure", params={"file_path": ".env"})
        assert status == 403
        status, _ = _get("/infrastructure", params={"file_path": "credentials.txt"})
        assert status == 403

    def test_writeback_on_single_writable_file_mount(self, tmp_path):
        """Exercise the deployed ro-directory/rw-file boundary with the nonroot image."""
        root = tmp_path / "ledger"
        root.mkdir(mode=0o755)
        book = root / "main.bean"
        book.write_text(
            '2024-01-01 open Assets:Cash USD\n'
            '2024-01-01 open Expenses:Food USD\n'
            'include "manual_transactions.bean"\n', encoding="utf-8",
        )
        book.chmod(0o644)
        manual = root / "manual_transactions.bean"
        manual.write_text("; retain this source comment\n", encoding="utf-8")
        manual.chmod(0o666)  # Synthetic fixture only: permit the container's app UID.
        before = {path.name: path.read_bytes() for path in root.iterdir()}
        name = f"{CONTAINER_NAME}-writeback"
        transaction = (
            '2026-05-28 * "Shop" "Lunch"\n'
            '  cashier_id: "container-writeback-security"\n'
            '  Assets:Cash -5 USD\n'
            '  Expenses:Food 5 USD\n'
        )
        try:
            subprocess.run(
                ["docker", "run", "-d", "--name", name,
                 "-p", "127.0.0.1::3000", "-v", f"{root}:/workspace:ro",
                 "-v", f"{manual}:/workspace/manual_transactions.bean:rw",
                 "-e", "BEANCOUNT_FILE=/workspace/main.bean",
                 "-e", "BEANCOUNT_MANUAL_TRANSACTIONS_FILE=/workspace/manual_transactions.bean",
                 "-e", "CASHIER_API_TOKEN", IMAGE_TAG],
                check=True, capture_output=True, timeout=30,
                env={**os.environ, "CASHIER_API_TOKEN": AUTH_CREDENTIAL},
            )
            binding = subprocess.run(
                ["docker", "port", name, "3000/tcp"],
                check=True, capture_output=True, text=True, timeout=10,
            ).stdout.strip()
            port = int(binding.rsplit(":", 1)[1])

            def request(path, payload=None):
                data = json.dumps(payload).encode() if payload is not None else None
                headers = {"Authorization": f"Bearer {AUTH_CREDENTIAL}"}
                if data is not None:
                    headers["Content-Type"] = "application/json"
                req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, headers=headers)
                with urllib.request.urlopen(req, timeout=15) as response:
                    assert response.status == 200
                    return json.load(response)

            deadline = time.monotonic() + 30
            while True:
                try:
                    assert request("/health")["beancount_loaded"] is True
                    break
                except (urllib.error.URLError, ConnectionError, OSError):
                    if time.monotonic() > deadline:
                        raise
                    time.sleep(0.2)
            expected = {"synchronized": ["container-writeback-security"], "rejected": []}
            assert request("/xact", {"transactions": [transaction, transaction]}) == expected
            committed = manual.read_bytes()
            assert committed.startswith(before[manual.name])
            assert committed.count(b"container-writeback-security") == 1
            assert request("/xact", {"transactions": [transaction]}) == expected
            assert manual.read_bytes() == committed
            assert request("/?query=" + quote("SELECT payee FROM transactions")) == [["Shop"]]
            pulled = request("/infrastructure?file_path=main.bean")["content"]
            assert 'cashier_id: "container-writeback-security"' in pulled
            assert '"Shop" "Lunch"' in pulled
            assert {path.name for path in root.iterdir()} == set(before)
            assert book.read_bytes() == before[book.name]
            boundary = subprocess.run(
                ["docker", "exec", name, "/app/.venv/bin/python", "-c",
                 "from pathlib import Path; Path('/workspace/forbidden.bean').write_text('not allowed')"],
                check=False, capture_output=True, text=True, timeout=10,
            )
            assert boundary.returncode != 0
            assert "Read-only file system" in boundary.stderr
        finally:
            subprocess.run(["docker", "rm", "-f", name], check=False, capture_output=True, timeout=30)

    def test_missing_token_container_refuses_startup(self):
        name = f"{CONTAINER_NAME}-missing-token"
        try:
            result = subprocess.run(
                ["docker", "run", "--name", name, "--network", "none",
                 "-v", f"{FIXTURE_DIR}:/workspace:ro",
                 "-e", "BEANCOUNT_FILE=/workspace/main.bean", IMAGE_TAG],
                check=False, capture_output=True, text=True, timeout=30,
            )
            assert result.returncode == 1, result.stdout + result.stderr
            assert "CASHIER_API_TOKEN must be configured" in result.stderr
            assert "Uvicorn running" not in result.stdout + result.stderr
            assert AUTH_CREDENTIAL not in result.stdout + result.stderr
        finally:
            subprocess.run(["docker", "rm", "-f", name], check=False, capture_output=True, timeout=30)

    def test_infrastructure_root_returns_http_200_not_422(self):
        status, body = _get("/infrastructure", params={"file_path": "main.bean"})
        assert status == 200, f"Expected 200, got {status}. Response: {body[:500]}"
        data = json.loads(body)
        assert "content" in data
        content = data["content"]

        # No raw top-level plugin directives remain
        assert 'plugin "' not in content, "Materialized snapshot still contains plugin directives"
        # No include directives remain
        assert 'include "' not in content, "Materialized snapshot still contains include directives"

        # Consumed top-level Pad directives are absent
        assert "pad " not in content, "Materialized snapshot still contains pad directives"

        # Semantic generated records preserved: Custom entries, transactions, balances
        assert "custom " in content, "Custom entries missing from materialized snapshot"
        assert "balance " in content, "Balance directives missing from materialized snapshot"
        assert "* " in content, "Transactions missing from materialized snapshot"

        # Specific fixture evidence
        assert "Assets:MyAutomaticBroker:Total" in content
        assert "Assets:MyFavouriteBank:Cash" in content
        assert "Equity:RegularTransacionForSummariesFrom" in content
        assert '"Rounded FX exact total"' in content
        assert "Assets:MyFavouriteBank:Cash -1000 GBP @@ 1234.56 USD" in content
        assert '"Unit FX price remains unit price"' in content
        assert "Assets:MyFavouriteBank:Cash -10 GBP @ 1.23 USD" in content

        # Production-like transforming plugins are active, not merely declared.
        # filter_map tags mapped operations and the recur/split plugins materialize
        # their transformed transaction forms in the standalone snapshot.
        assert 'custom "filter-map" "apply" "recurring" "#subscription-year"' in content
        assert '"Monthly rent" #recurring' in content
        assert re.search(
            r'"Regular transaction for summaries \(recur 1/\d+\)" #auxiliary #recurred',
            content,
        )
        assert '"Subscription for SomeService paid in a year-instalment (split 1/12)"' in content
        assert "#splitted" in content

    def test_infrastructure_includes_supporting_files(self):
        status, body = _get("/infrastructure", params={"file_path": "accounts.bean"})
        assert status == 200
        data = json.loads(body)
        assert "Expenses:EatingOut" in data["content"]

        status, body = _get("/infrastructure", params={"file_path": "commodities.bean"})
        assert status == 200
        data = json.loads(body)
        assert "AUTO_BROKER_USD" in data["content"]

    def test_materialized_content_reparses(self):
        """The materialized snapshot must be valid Python-Beancount text."""
        status, body = _get("/infrastructure", params={"file_path": "main.bean"})
        assert status == 200
        content = json.loads(body)["content"]

        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", suffix=".bean", delete=False) as f:
            f.write(content)
            tmp_path = f.name
        try:
            from beancount.parser import parser
            reparsed, errors, _ = parser.parse_file(tmp_path)
            assert not errors, f"Parser errors in materialized snapshot: {[str(e) for e in errors]}"
            assert len(reparsed) > 0, "Materialized snapshot produced no entries"
        finally:
            os.unlink(tmp_path)

    def test_materialized_content_parses_with_pwa_rustledger_engine(self):
        """The actual PWA WASM engine must accept the standalone snapshot with 0 errors."""
        script = os.path.join(os.path.dirname(__file__), "acceptance", "parse_with_rustledger.mjs")
        result = subprocess.run(
            ["node", script],
            check=False,
            env={**os.environ, "PORT": str(HOST_PORT), "CASHIER_API_TOKEN": AUTH_CREDENTIAL},
            text=True,
            capture_output=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "RustLedger parsed materialized snapshot with 0 errors" in result.stdout
        assert "RustLedger rejects lossy @@ -> @ rewrite with" in result.stdout

    def test_materialized_content_has_no_invalid_private_metadata(self):
        """Invalid private metadata (_-prefixed keys) must be canonicalized away."""
        status, body = _get("/infrastructure", params={"file_path": "main.bean"})
        assert status == 200
        content = json.loads(body)["content"]
        lines_with_underscore_meta = [
            l for l in content.splitlines()
            if l.strip().startswith("_") and ": " in l
        ]
        assert len(lines_with_underscore_meta) == 0, (
            f"Found private metadata keys in output: {lines_with_underscore_meta}"
        )

    def test_materialized_has_parseable_metadata_values(self):
        """Enum and other programmatic metadata values must be serialized parseably."""
        status, body = _get("/infrastructure", params={"file_path": "main.bean"})
        assert status == 200
        content = json.loads(body)["content"]

        # The valuation plugin produces entries with metadata like asset_class.
        # Check that commas and key: value pairs parse correctly.
        import tempfile

        from beancount.parser import parser
        with tempfile.NamedTemporaryFile(mode="w", suffix=".bean", delete=False) as f:
            f.write(content)
            tmp_path = f.name
        try:
            reparsed, errors, _ = parser.parse_file(tmp_path)
            assert not errors, f"Parser errors with metadata: {[str(e) for e in errors]}"
            # Every transaction's postings must have parseable metadata
            from beancount.core import data
            for entry in reparsed:
                assert entry.meta is not None
                if isinstance(entry, data.Transaction):
                    for posting in entry.postings:
                        assert posting.meta is not None
        finally:
            os.unlink(tmp_path)

    def test_materialized_keeps_padding_transactions(self):
        """Padding transactions generated from pad directives must survive materialization."""
        status, body = _get("/infrastructure", params={"file_path": "main.bean"})
        assert status == 200
        content = json.loads(body)["content"]

        import tempfile

        from beancount.core import data
        from beancount.parser import parser

        with tempfile.NamedTemporaryFile(mode="w", suffix=".bean", delete=False) as f:
            f.write(content)
            tmp_path = f.name
        try:
            reparsed, errors, _ = parser.parse_file(tmp_path)
            assert not errors
            # Should have transaction entries
            transactions = [e for e in reparsed if isinstance(e, data.Transaction)]
            assert len(transactions) > 0, "No transactions in materialized snapshot"
            # Should have balance entries
            balances = [e for e in reparsed if isinstance(e, data.Balance)]
            assert len(balances) > 0, "No balance entries in materialized snapshot"
            # Should have custom entries (budget, valuation, filter-map)
            customs = [e for e in reparsed if isinstance(e, data.Custom)]
            assert len(customs) > 0, "No custom entries in materialized snapshot"
            # Should NOT have Pad entries
            pads = [e for e in reparsed if isinstance(e, data.Pad)]
            assert len(pads) == 0, "Pad entries leaked into materialized snapshot"
        finally:
            os.unlink(tmp_path)

    def test_image_has_required_plugin_modules(self):
        """Verify all required plugin modules are importable inside the built image."""
        modules = [
            "beancount_lazy_plugins.valuation",
            "beancount_lazy_plugins.generate_inverse_prices",
            "beancount_lazy_plugins.generate_base_ccy_prices",
            "beancount_lazy_plugins.group_pad_transactions",
            "beancount_lazy_plugins.auto_accounts",
            "beancount_lazy_plugins.filter_map",
            "beancount_share.share",
            "beancount_reds_plugins.effective_date.effective_date",
            "beancount_interpolate.recur",
            "beancount_interpolate.split",
        ]
        script = "; ".join(f"import {m}" for m in modules)
        result = subprocess.run(
            ["docker", "run", "--rm", IMAGE_TAG, "python", "-c", script],
            check=False, capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            raise AssertionError(
                f"Plugin import check failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
            )

    def test_materialized_content_reparses_with_rustledger(self):
        """Materialized snapshot must parse with 0 errors in @rustledger/wasm (PWA boundary)."""
        script = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "acceptance", "parse_with_rustledger.mjs"
        )
        result = subprocess.run(
            ["node", script],
            check=False, capture_output=True, text=True, timeout=30,
            env={**os.environ, "PORT": str(HOST_PORT), "CASHIER_API_TOKEN": AUTH_CREDENTIAL},
        )
        if result.returncode != 0:
            raise AssertionError(
                f"RustLedger parse check failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
            )


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
