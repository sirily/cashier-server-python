"""Container-level acceptance tests for production Docker image.

Builds the clean Docker image from the repo and tests it against
a vendored plugin-bearing ledger fixture representative of the
Lazy Beancount workspace.  No runtime pip install / host venv
mount / separate QA image — the test uses only the PR-built image.
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import quote

import pytest

FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "plugin_ledger")
IMAGE_TAG = "cashier-server-acceptance:test"
CONTAINER_NAME = "cashier-server-acceptance-test"
HOST_PORT = 3099

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
    subprocess.run(
        [
            "docker", "run", "--rm", "-d",
            "--name", CONTAINER_NAME,
            "-p", f"{HOST_PORT}:3000",
            "-v", f"{FIXTURE_DIR}:/workspace:ro",
            "-e", "BEANCOUNT_FILE=/workspace/main.bean",
            IMAGE_TAG,
        ],
        check=True,
        capture_output=True,
    )


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
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return None


def _get(path, params=None):
    url = f"http://127.0.0.1:{HOST_PORT}{path}"
    if params:
        qs = "&".join(f"{k}={quote(v)}" for k, v in params.items())
        url = f"{url}?{qs}"
    resp = urllib.request.urlopen(url, timeout=10)
    return resp.status, resp.read().decode("utf-8")


def _stop_container():
    subprocess.run(["docker", "stop", CONTAINER_NAME], capture_output=True, timeout=30)
    subprocess.run(["docker", "rm", "-f", CONTAINER_NAME], capture_output=True, timeout=30)


def _remove_image():
    subprocess.run(["docker", "rmi", "-f", IMAGE_TAG], capture_output=True, timeout=30)


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

    def test_health_ok(self):
        status, body = _get("/health")
        assert status == 200
        data = json.loads(body)
        assert data["ok"] is True
        assert data["beancount_file_configured"] is True
        assert data["beancount_loaded"] is True

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
            env={**os.environ, "PORT": str(HOST_PORT)},
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
        from beancount.parser import parser
        import tempfile
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
            capture_output=True, text=True, timeout=30,
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
            capture_output=True, text=True, timeout=30,
            env={**os.environ, "PORT": str(HOST_PORT)},
        )
        if result.returncode != 0:
            raise AssertionError(
                f"RustLedger parse check failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
            )


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
