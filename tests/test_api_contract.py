"""Check the versioned OpenAPI artifact against the app and real HTTP behavior."""


import json
import threading
from decimal import Decimal
from pathlib import Path

import pytest
from beancount import loader
from beancount.core import data
from fastapi.openapi.models import OpenAPI
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

import main
import writeback
from cashier_snapshot import http_limits, workspace

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = json.loads((ROOT / "docs/api/openapi-v2.json").read_text(encoding="utf-8"))


def validate_value(value, schema):
    Draft202012Validator({**schema, "components": CONTRACT["components"]}).validate(value)


def validate_response(response, path, method="get"):
    operation = CONTRACT["paths"][path][method]
    documented = operation["responses"][str(response.status_code)]
    media_type = response.headers["content-type"].split(";")[0]
    schema = documented["content"][media_type]["schema"]
    validate_value(response.json() if media_type == "application/json" else response.text, schema)
    for name, header in documented.get("headers", {}).items():
        assert name in response.headers
        validate_value(response.headers[name], header["schema"])
    return response.json()


@pytest.fixture
def sync_api(monkeypatch, tmp_path):
    root = tmp_path / "main.bean"
    source = (ROOT / "tests/fixtures/sync-contract.bean").read_text(encoding="utf-8")
    root.write_text(source + '\ninclude "manual_transactions.bean"\n', encoding="utf-8")
    manual = tmp_path / "manual_transactions.bean"
    manual.write_text("", encoding="utf-8")
    monkeypatch.setattr(main, "BEAN_FILE", str(root))
    monkeypatch.setenv("BEANCOUNT_FILE", str(root))
    monkeypatch.setenv("BEANCOUNT_MANUAL_TRANSACTIONS_FILE", str(manual))
    monkeypatch.setattr(main, "CASHIER_API_TOKEN", "contract-test-token")

    with TestClient(main.app, headers={"Authorization": "Bearer contract-test-token"}) as client:
        yield client, root, manual


def test_openapi_artifact_matches_implementation_and_has_valid_schemas():
    main.app.openapi_schema = None
    assert main.app.openapi() == CONTRACT
    OpenAPI.model_validate(CONTRACT)
    assert CONTRACT["openapi"].startswith("3.1.")
    assert CONTRACT["x-contract-version"] == "2.0.0"
    assert len(CONTRACT["servers"]) == 1
    server = CONTRACT["servers"][0]
    assert server["url"] == "/"
    assert "deployment" in server["description"].lower()
    assert set(CONTRACT["paths"]) == {"/infrastructure", "/xact", "/health", "/ping"}
    for schema in CONTRACT["components"]["schemas"].values():
        Draft202012Validator.check_schema(schema)

    def check_references(node):
        if isinstance(node, dict):
            if "$ref" in node:
                assert node["$ref"].startswith("#/")
                target = CONTRACT
                for component in node["$ref"][2:].split("/"):
                    target = target[component.replace("~1", "/").replace("~0", "~")]
            for value in node.values():
                check_references(value)
        elif isinstance(node, list):
            for value in node:
                check_references(value)

    check_references(CONTRACT)


def test_contract_security_and_parameters():
    scheme = CONTRACT["components"]["securitySchemes"]["CashierBearer"]
    assert scheme == {"type": "http", "scheme": "bearer"}
    for path, methods in CONTRACT["paths"].items():
        for operation in methods.values():
            assert operation["security"] == ([] if path == "/health" else [{"CashierBearer": []}])
    path = CONTRACT["paths"]["/infrastructure"]["get"]["parameters"][0]
    assert path["name"] == "file_path" and path["required"]
    body = CONTRACT["paths"]["/xact"]["post"]["requestBody"]
    assert body["required"]
    assert CONTRACT["components"]["schemas"]["XactRequest"]["properties"]["transactions"]["maxItems"] == 100


def test_contract_limits_are_bound_to_implementation():
    limits = CONTRACT["x-cashier-limits"]
    assert not any(name.startswith("bql_") for name in limits)
    for name, actual in {
        "request_body_bytes": http_limits.MAX_REQUEST_BYTES,
        "query_string_bytes": http_limits.MAX_QUERY_STRING_BYTES,
        "body_receive_seconds": http_limits.BODY_TIMEOUT_SECONDS,
        "xact_batch_items": writeback.MAX_TRANSACTIONS,
        "xact_transaction_bytes": writeback.MAX_TRANSACTION_BYTES,
        "xact_postings": writeback.MAX_POSTINGS,
        "xact_metadata_fields": writeback.MAX_METADATA,
        "xact_metadata_key_characters": writeback.MAX_METADATA_KEY_LENGTH,
        "xact_metadata_string_bytes": writeback.MAX_METADATA_STRING_BYTES,
        "cashier_id_characters": writeback.MAX_CASHIER_ID_LENGTH,
        "ledger_file_bytes": workspace.MAX_FILE_BYTES,
        "ledger_capture_bytes": workspace.MAX_WORKSPACE_BYTES,
        "ledger_files": workspace.MAX_WORKSPACE_FILES,
        "ledger_include_visits": workspace.MAX_INCLUDE_VISITS,
        "ledger_include_depth": workspace.MAX_INCLUDE_DEPTH,
        "ledger_expanded_bytes": workspace.MAX_EXPANDED_BYTES,
    }.items():
        assert limits[name] == actual, name


@pytest.mark.parametrize("path", ["/", "/?query=SELECT+1", "/reload"])
def test_removed_operations_are_not_found_or_documented(sync_api, path):
    client, _, _ = sync_api
    response = client.get(path)
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}
    assert path.split("?")[0] not in CONTRACT["paths"]


def test_removed_bql_and_cache_state_are_absent(sync_api):
    client, _, _ = sync_api
    assert client.get("/ping").status_code == 200
    for name in (
        "index", "beancount", "preload_beancount_data", "reload_beancount_data",
        "refresh_beancount_connection", "commit_and_refresh",
    ):
        assert not hasattr(main, name)
    assert not hasattr(main.app.state, "connection")


def test_root_snapshot_and_health_responses_follow_contract(sync_api):
    client, _, _ = sync_api
    response = client.get("/infrastructure", params={"file_path": "main.bean"})
    assert response.status_code == 200
    content = validate_response(response, "/infrastructure")["content"]
    assert 'include "' not in content and 'plugin "' not in content
    entries, errors, _ = loader.load_string(content)
    assert not errors
    assert sum(isinstance(entry, data.Transaction) for entry in entries) == 4
    response = client.get("/health", headers={"Authorization": ""})
    assert response.status_code == 200
    assert validate_response(response, "/health") == {
        "ok": True, "beancount_file_configured": True,
    }
    assert validate_response(client.get("/ping"), "/ping") == "pong"



def test_writeback_total_price_partial_success_retry_and_conflict_follow_contract(sync_api):
    client, _, manual = sync_api
    transaction = (
        '2024-01-06 * "FX" "Exact total"\n'
        '  cashier_id: "contract-exact-total"\n'
        '  Assets:Foreign -3 GBP @@ 1.00 USD\n'
        '  Assets:Cash 1.00 USD\n'
    )
    invalid = transaction.replace("contract-exact-total", "contract-invalid").replace("Assets:Cash", "Assets:Unknown")
    request = {"transactions": [transaction, invalid]}
    request_schema = CONTRACT["paths"]["/xact"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    validate_value(request, request_schema)
    response = client.post("/xact", json=request)
    assert response.status_code == 200
    result = validate_response(response, "/xact", "post")
    assert result["synchronized"] == ["contract-exact-total"]
    assert len(result["rejected"]) == 1
    assert result["rejected"][0]["cashier_id"] == "contract-invalid"
    assert "Unknown account" in result["rejected"][0]["reason"]
    committed = manual.read_bytes()
    writeback._check_writeback_integrity(committed)
    assert b"@@ 1.00 USD" in committed and b" @ " not in committed
    retry = client.post("/xact", json={"transactions": [transaction]})
    assert retry.status_code == 200
    assert validate_response(retry, "/xact", "post") == {
        "synchronized": ["contract-exact-total"], "rejected": [],
    }
    assert manual.read_bytes() == committed
    conflict = client.post("/xact", json={"transactions": [transaction.replace('"Exact total"', '"Changed"')]})
    assert conflict.status_code == 200
    conflict_result = validate_response(conflict, "/xact", "post")
    assert conflict_result["synchronized"] == []
    assert conflict_result["rejected"][0]["cashier_id"] == "contract-exact-total"
    assert "Conflict" in conflict_result["rejected"][0]["reason"]
    assert manual.read_bytes() == committed
    pulled = client.get("/infrastructure", params={"file_path": "main.bean"})
    content = validate_response(pulled, "/infrastructure")["content"]
    assert content.count('cashier_id: "contract-exact-total"') == 1
    assert "@@ 1.00 USD" in content and " @ " not in content
    entries, errors, _ = loader.load_string(content)
    assert not errors
    matching = [entry for entry in entries if isinstance(entry, data.Transaction) and entry.meta.get("cashier_id") == "contract-exact-total"]
    assert len(matching) == 1
    foreign_units = matching[0].postings[0].units
    cash_units = matching[0].postings[1].units
    assert foreign_units is not None and cash_units is not None
    assert foreign_units.number == Decimal(-3)
    assert cash_units.number == Decimal("1.00")



@pytest.mark.parametrize("path,method,params,body", [

    ("/infrastructure", "get", {"file_path": "main.bean"}, None),
    ("/xact", "post", None, {"transactions": []}),
    ("/ping", "get", None, None),
])
def test_protected_operations_require_bearer_as_documented(sync_api, path, method, params, body):
    client, _, _ = sync_api
    response = client.request(method, path, params=params, json=body, headers={"Authorization": ""})
    assert response.status_code == 401
    validate_response(response, path, method)


@pytest.mark.parametrize("file_path,status", [("accounts.bean", 403), ("*.bean", 403), ("", 404), ("x" * 1025, 400)])
def test_snapshot_path_errors_follow_contract(sync_api, file_path, status):
    client, _, _ = sync_api
    response = client.get("/infrastructure", params={"file_path": file_path})
    assert response.status_code == status
    validate_response(response, "/infrastructure")


def test_schema_validation_and_build_errors_follow_contract(sync_api):
    client, root, _ = sync_api
    response = client.get("/infrastructure")
    assert response.status_code == 422
    validate_response(response, "/infrastructure")
    for body in ({}, {"transactions": [1]}, {"transactions": ["invalid"] * 101}):
        response = client.post("/xact", json=body)
        assert response.status_code == 422
        validate_response(response, "/xact", "post")
    root.write_text('include "missing.bean"\n', encoding="utf-8")
    response = client.get("/infrastructure", params={"file_path": "main.bean"})
    assert response.status_code == 422
    assert validate_response(response, "/infrastructure")["detail"]["errors"]


def test_admission_configuration_and_http_limits_follow_contract(sync_api, monkeypatch):
    client, _, _ = sync_api
    with monkeypatch.context() as patch:
        gate = threading.BoundedSemaphore(1)
        gate.acquire()
        patch.setattr(main, "_heavy_operations", gate)
        response = client.get("/infrastructure", params={"file_path": "main.bean"})
        assert response.status_code == 503
        validate_response(response, "/infrastructure")
    with monkeypatch.context() as patch:
        patch.setattr(main, "BEAN_FILE", None)
        response = client.get("/infrastructure", params={"file_path": "main.bean"})
        assert response.status_code == 500
        validate_response(response, "/infrastructure")
        response = client.get("/health", headers={"Authorization": ""})
        assert response.status_code == 200
        assert validate_response(response, "/health") == {
            "ok": True, "beancount_file_configured": False,
        }
    with monkeypatch.context() as patch:
        patch.setattr(main, "CASHIER_API_TOKEN", "")
        response = client.get("/ping")
        assert response.status_code == 503
        validate_response(response, "/ping")
    response = client.get("/health", headers={"Host": "bad/health"})
    assert response.status_code == 400
    validate_response(response, "/health")
    response = client.post("/xact", content=b"x" * (http_limits.MAX_REQUEST_BYTES + 1))
    assert response.status_code == 413
    validate_response(response, "/xact", "post")
    monkeypatch.setattr(http_limits, "MAX_QUERY_STRING_BYTES", 64)
    response = client.get("/infrastructure", params={"file_path": "x" * 65})
    assert response.status_code == 413
    validate_response(response, "/infrastructure")


def test_response_validator_rejects_contract_drift():
    schema = CONTRACT["paths"]["/health"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    valid = {"ok": True, "beancount_file_configured": True}
    validate_value(valid, schema)
    invalid = {**valid, "ok": "yes"}
    assert not Draft202012Validator({**schema, "components": CONTRACT["components"]}).is_valid(invalid)
