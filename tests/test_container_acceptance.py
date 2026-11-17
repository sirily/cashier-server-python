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
import textwrap
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

    @pytest.mark.parametrize("path", ["/", "/?query=SELECT%201", "/reload", "/hello", "/shutdown"])
    def test_removed_endpoints_return_404(self, path):
        status, body = _get(path)
        assert status == 404
        assert json.loads(body) == {"detail": "Not Found"}

    def test_missing_beancount_file_container_refuses_startup(self):
        name = f"{CONTAINER_NAME}-missing-beancount-file"
        try:
            result = subprocess.run(
                ["docker", "run", "--name", name, "--network", "none",
                 "-v", f"{FIXTURE_DIR}:/workspace:ro",
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
        assert json.loads(body) == {"ok": True, "beancount_file_configured": True}

    @pytest.mark.parametrize("authorization", [None, "Bearer wrong-token", "Basic wrong-token", "Bearer "])
    @pytest.mark.parametrize("path,method,data", [
        ("/ping", "GET", None),
        ("/infrastructure?file_path=main.bean", "GET", None),
        ("/xact", "POST", b'{"transactions": []}'),
        ("/openapi.json", "GET", None),
    ])
    def test_invalid_auth_rejected(self, authorization, path, method, data):
        status, body = _get(path, authorization=authorization, method=method, data=data)
        assert status == 401
        assert json.loads(body) == {"detail": "Invalid or missing API token"}
        assert AUTH_CREDENTIAL not in body

    @pytest.mark.parametrize("host", ["testserver/health?", "testserver/health#"])
    @pytest.mark.parametrize("path,method,data", [
        ("/infrastructure?file_path=main.bean", "GET", None),
        ("/ping", "GET", None),
        ("/xact", "POST", b'{"transactions": []}'),
    ])
    def test_malformed_host_never_bypasses_authentication(self, host, path, method, data):
        status, _ = _get(path, authorization=None, method=method, data=data, host=host)
        assert status == 400

    def test_runtime_openapi_matches_versioned_contract(self):
        status, body = _get("/openapi.json")
        assert status == 200
        contract = os.path.join(os.path.dirname(__file__), "..", "docs", "api", "openapi-v2.json")
        with open(contract, encoding="utf-8") as source:
            assert json.loads(body) == json.load(source)

    def test_infrastructure_rejects_hidden_nonledger_paths(self):
        status, _ = _get("/infrastructure", params={"file_path": ".env"})
        assert status == 403
        status, _ = _get("/infrastructure", params={"file_path": "credentials.txt"})
        assert status == 403

    @pytest.mark.parametrize("account,amount,expense_amount", [
        pytest.param("Assets:Cash", "-5 USD", "5 USD", id="single-currency"),
        pytest.param("Assets:Foreign", "-3 GBP @@ 1.00 USD", "1.00 USD", id="total-price"),
        pytest.param(
            "Assets:Foreign", "-3 GBP @@ 0.1234567890123456789012345678 USD",
            "0.1234567890123456789012345678 USD", id="high-precision-total-price",
        ),
        pytest.param("Assets:Foreign", "-3 GBP @ 0.50 USD", "1.50 USD", id="unit-price"),
    ])
    def test_writeback_on_single_writable_file_mount(self, tmp_path, account, amount, expense_amount):
        """Exercise the deployed ro-directory/rw-file boundary with the nonroot image."""
        root = tmp_path / "ledger"
        root.mkdir(mode=0o755)
        book = root / "main.bean"
        book.write_text(
            '2024-01-01 open Assets:Cash USD\n'
            '2024-01-01 open Assets:Foreign GBP\n'
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
            f'  {account} {amount}\n'
            f'  Expenses:Food {expense_amount}\n'
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
                    assert request("/health")["ok"] is True
                    break
                except (urllib.error.URLError, ConnectionError, OSError):
                    if time.monotonic() > deadline:
                        raise
                    time.sleep(0.2)
            initial_snapshot = request("/infrastructure?file_path=main.bean")["content"]
            assert 'cashier_id: "container-writeback-security"' not in initial_snapshot
            expected = {"synchronized": ["container-writeback-security"], "rejected": []}
            assert request("/xact", {"transactions": [transaction, transaction]}) == expected
            committed = manual.read_bytes()
            assert committed.startswith(before[manual.name])
            assert committed.count(b"container-writeback-security") == 1
            posting_pattern = rf"^\s+{re.escape(account)}\s+{re.escape(amount)}$"
            assert re.search(posting_pattern, committed.decode("utf-8"), re.MULTILINE)
            pulled = request("/infrastructure?file_path=main.bean")["content"]
            assert pulled != initial_snapshot
            assert pulled.count('cashier_id: "container-writeback-security"') == 1
            assert '"Shop" "Lunch"' in pulled
            assert re.search(posting_pattern, pulled, re.MULTILINE)
            assert request("/xact", {"transactions": [transaction]}) == expected
            assert manual.read_bytes() == committed
            assert request("/infrastructure?file_path=main.bean")["content"] == pulled
            from beancount.core import data
            from beancount.parser import parser

            entries, errors, _ = parser.parse_string(pulled)
            assert not errors, errors
            transactions = [entry for entry in entries if isinstance(entry, data.Transaction)]
            assert len(transactions) == 1
            assert transactions[0].meta["cashier_id"] == "container-writeback-security"
            assert transactions[0].payee == "Shop"
            assert transactions[0].narration == "Lunch"
            assert [posting.account for posting in transactions[0].postings] == [account, "Expenses:Food"]
            result = subprocess.run(
                ["node", "--input-type=module", "-e", '''
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { initSync, Ledger } from "@rustledger/wasm";

initSync({ module: readFileSync("node_modules/@rustledger/wasm/rustledger_wasm_bg.wasm") });
const content = readFileSync(0, "utf8");
const ledger = Ledger.fromFiles({ "main.bean": content }, "main.bean");
const errors = ledger.getErrors();
assert.equal(errors.length, 0, JSON.stringify(errors));
'''],
                cwd=os.path.join(os.path.dirname(__file__), "acceptance"),
                input=pulled, check=False, text=True, capture_output=True, timeout=30,
            )
            assert result.returncode == 0, result.stdout + result.stderr
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

    def test_infrastructure_exposes_supporting_entries_only_in_root_snapshot(self):
        for file_path in (
            "accounts.bean", "commodities.bean", "manual_transactions.bean",
            "prices/*.bean", "*", "*.bean", "main.*", "main.bea?", "[m]ain.bean",
        ):
            status, body = _get("/infrastructure", params={"file_path": file_path})
            assert status == 403, (file_path, body)
            assert json.loads(body) == {
                "detail": "Only the configured root snapshot is available"
            }

        status, body = _get("/infrastructure", params={"file_path": "main.bean"})
        assert status == 200, body
        data = json.loads(body)
        assert set(data) == {"content"}
        content = data["content"]
        assert "1970-01-01 open Expenses:EatingOut" in content
        assert "1970-01-01 commodity AUTO_BROKER_USD" in content
        assert '"Salary from MyEmployer"' in content
        assert "2024-01-02 price GBP 1.27 USD" in content
        assert 'include "' not in content
        assert 'plugin "' not in content

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

    def test_image_installs_application_noneditable_in_venv(self):
        script = textwrap.dedent("""\
            import importlib
            import importlib.metadata
            import json
            import os
            from pathlib import Path
            import sys

            assert os.getuid() != 0, "Runtime user must not be root"
            prefix = Path(sys.prefix).resolve()
            assert prefix == Path("/app/.venv"), prefix
            for name in ("main", "writeback", "cashier_snapshot"):
                module = importlib.import_module(name)
                location = Path(module.__file__).resolve()
                assert location.is_relative_to(prefix), (name, location)
                assert "site-packages" in location.parts, (name, location)
            distribution = importlib.metadata.distribution("cashier-server")
            assert Path(distribution.locate_file("")).resolve().is_relative_to(prefix)
            direct_url = json.loads(distribution.read_text("direct_url.json") or "{}")
            assert not direct_url.get("dir_info", {}).get("editable"), direct_url
            for name in (
                "main.py", "writeback.py", "cashier_snapshot", "pyproject.toml",
                "uv.lock", "README.md", ".git", "build", "dist", ".cache",
            ):
                path = Path("/app") / name
                assert not path.exists(), f"Build/source artifact retained: {path}"
            """)
        result = subprocess.run(
            ["docker", "exec", CONTAINER_NAME, "/app/.venv/bin/python", "-c", script],
            check=False, capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, (
            f"Non-editable installation check failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )

    @pytest.mark.parametrize("python", ["/app/.venv/bin/python", "/usr/local/bin/python"])
    def test_image_excludes_build_and_development_tools(self, python):
        # Root can inspect /root/.cache; the application itself is checked as its runtime user above.
        script = textwrap.dedent("""\
            import importlib.metadata
            import importlib.util
            from pathlib import Path
            import shutil

            for command in (
                "uv", "uvx", "git", "pip", "pip3", "pytest", "py.test",
                "ruff", "basedpyright", "basedpyright-langserver", "node", "npm",
            ):
                assert shutil.which(command) is None, f"Unexpected executable: {command}"
            for module in ("pip", "uv", "pytest", "pytest_asyncio", "pytest_timeout", "ruff", "basedpyright", "jsonschema"):
                assert importlib.util.find_spec(module) is None, f"Unexpected module: {module}"
            for name in ("pip", "uv", "pytest", "pytest-asyncio", "pytest-timeout", "ruff", "basedpyright", "jsonschema"):
                try:
                    importlib.metadata.distribution(name)
                except importlib.metadata.PackageNotFoundError:
                    pass
                else:
                    raise AssertionError(f"Unexpected distribution: {name}")
            for path in ("/root/.cache/uv", "/app/.cache/uv"):
                assert not Path(path).exists(), f"Build cache retained: {path}"
            """)
        result = subprocess.run(
            ["docker", "exec", "--user", "0", CONTAINER_NAME, python, "-c", script],
            check=False, capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, (
            f"Runtime tool isolation check failed ({python}):\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_image_has_required_plugin_modules(self):
        """Verify all required plugin modules are importable inside the built image."""
        modules = [
            "fava",
            "fava.core.filters",
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

    def test_image_filter_map_uses_fava_filters(self):
        """Removing the query API must not break filter_map's runtime Fava dependency."""
        script = textwrap.dedent(r'''
            from beancount import loader
            from beancount.core.data import Transaction

            source = (
                'plugin "beancount_lazy_plugins.filter_map"\n'
                '2024-01-01 open Assets:Cash USD\n'
                '2024-01-01 open Expenses:Food USD\n'
                '2024-01-01 custom "filter-map" "apply"\n'
                '  filter: "narration:\'Matched purchase\'"\n'
                '  addTags: "filtered"\n'
                '2024-01-02 * "Shop" "Matched purchase"\n'
                '  Assets:Cash -1 USD\n'
                '  Expenses:Food 1 USD\n'
                '2024-01-03 * "Shop" "Other purchase"\n'
                '  Assets:Cash -2 USD\n'
                '  Expenses:Food 2 USD\n'
            )
            entries, errors, _ = loader.load_string(source)
            assert not errors, errors
            transactions = [entry for entry in entries if isinstance(entry, Transaction)]
            assert len(transactions) == 2, transactions
            assert transactions[0].narration == "Matched purchase"
            assert transactions[0].tags == frozenset({"filtered"}), transactions[0]
            assert transactions[1].narration == "Other purchase"
            assert not transactions[1].tags, transactions[1]
            ''')
        result = subprocess.run(
            ["docker", "run", "--rm", "--network", "none", IMAGE_TAG,
             "/app/.venv/bin/python", "-c", script],
            check=False, capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, (
            f"Fava filter_map runtime check failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
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
