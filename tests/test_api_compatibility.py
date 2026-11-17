"""Compatibility policy tests; this module never imports or starts the API app."""

import hashlib
import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from scripts import check_api_contract as checker
from scripts.check_api_contract import breaking_changes

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/check_api_contract.py"


@pytest.fixture
def contract():
    return {
        "openapi": "3.1.0",
        "info": {"title": "Cashier", "version": "2.0.0"},
        "x-contract-version": "2.0.0",
        "servers": [{"url": "/"}],
        "x-cashier-limits": {
            "ledger_file_bytes": 16777216,
            "uvicorn_concurrency": 64,
            "body_receive_seconds": 15,
            "nested": {"per_file_max": 100},
        },
        "components": {
            "schemas": {
                "Input": {
                    "type": "object",
                    "required": ["name"],
                    "properties": {
                        "name": {"type": "string", "minLength": 1, "maxLength": 100},
                        "count": {"type": "integer", "minimum": 0, "maximum": 100},
                        "kind": {"type": "string", "enum": ["a", "b"]},
                    },
                },
                "Output": {
                    "type": "object",
                    "required": ["id"],
                    "properties": {
                        "id": {"type": "string"},
                        "note": {"type": "string"},
                        "kind": {"type": "string", "enum": ["a", "b"]},
                        "count": {"type": "integer", "minimum": 0, "maximum": 100},
                    },
                },
            },
            "securitySchemes": {"Bearer": {"type": "http", "scheme": "bearer"}},
        },
        "paths": {
            "/items": {
                "get": {
                    "parameters": [
                        {"name": "q", "in": "query", "schema": {"type": "string"}}
                    ],
                    "security": [{"Bearer": []}],
                    "responses": {
                        "200": {
                            "description": "OK",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Output"}
                                }
                            },
                        },
                        "400": {"description": "Invalid"},
                    },
                },
                "post": {
                    "security": [{"Bearer": []}],
                    "requestBody": {
                        "required": False,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/Input"}
                            }
                        },
                    },
                    "responses": {"204": {"description": "Created"}},
                },
            }
        },
    }


def get(document):
    return document["paths"]["/items"]["get"]


def post(document):
    return document["paths"]["/items"]["post"]


def input_schema(document):
    return document["components"]["schemas"]["Input"]


def output_schema(document):
    return document["components"]["schemas"]["Output"]


def response_media(document):
    return get(document)["responses"]["200"]["content"]["application/json"]


@pytest.mark.parametrize("method", [None, "get", "post"])
def test_removed_paths_and_methods(contract, method):
    current = deepcopy(contract)
    if method is None:
        del current["paths"]["/items"]
    else:
        del current["paths"]["/items"][method]
    changes = breaking_changes(contract, current)
    assert any("/items" in change and "removed" in change for change in changes)


@pytest.mark.parametrize("location", ["query", "header", "cookie", "path"])
def test_added_required_request_parameter(contract, location):
    current = deepcopy(contract)
    get(current)["parameters"].append(
        {"name": "new", "in": location, "required": True, "schema": {"type": "string"}}
    )
    assert any(
        "required request parameter" in change
        for change in breaking_changes(contract, current)
    )


def test_optional_parameter_becomes_required(contract):
    current = deepcopy(contract)
    get(current)["parameters"][0]["required"] = True
    assert breaking_changes(contract, current)


def test_required_request_body(contract):
    current = deepcopy(contract)
    post(current)["requestBody"]["required"] = True
    assert any(
        "required request body" in change
        for change in breaking_changes(contract, current)
    )
    del post(contract)["requestBody"]
    assert breaking_changes(contract, current)


@pytest.mark.parametrize("existing", [False, True])
def test_added_required_request_property(contract, existing):
    current = deepcopy(contract)
    name = "kind" if existing else "new"
    input_schema(current)["required"].append(name)
    input_schema(current)["properties"][name] = {"type": "string"}
    assert any(
        f"required request property '{name}' added" in change
        for change in breaking_changes(contract, current)
    )


@pytest.mark.parametrize(
    ("name", "patch"),
    [
        ("name", {"type": "integer"}),
        ("kind", {"enum": ["a"]}),
        ("count", {"minimum": 1}),
        ("count", {"maximum": 99}),
        ("name", {"minLength": 2}),
        ("name", {"maxLength": 99}),
        ("name", {"pattern": "^[a-z]+$"}),
        ("name", {"format": "uuid"}),
        ("count", {"multipleOf": 2}),
        ("kind", {"const": "a"}),
    ],
)
def test_narrowed_or_unknown_request_schema(contract, name, patch):
    current = deepcopy(contract)
    input_schema(current)["properties"][name].update(patch)
    assert breaking_changes(contract, current)


@pytest.mark.parametrize(
    ("key", "before", "after"),
    [
        ("minItems", 1, 2),
        ("maxItems", 10, 9),
        ("minProperties", 1, 2),
        ("maxProperties", 10, 9),
        ("exclusiveMinimum", 1, 2),
        ("exclusiveMaximum", 10, 9),
    ],
)
def test_all_numeric_request_bounds(contract, key, before, after):
    input_schema(contract)[key] = before
    current = deepcopy(contract)
    input_schema(current)[key] = after
    assert any(key in change for change in breaking_changes(contract, current))


@pytest.mark.parametrize(
    ("name", "patch"),
    [
        ("id", {"type": "integer"}),
        ("kind", {"enum": ["a", "b", "c"]}),
        ("count", {"minimum": -1}),
        ("count", {"maximum": 101}),
    ],
)
def test_incompatible_response_schema(contract, name, patch):
    current = deepcopy(contract)
    output_schema(current)["properties"][name].update(patch)
    assert breaking_changes(contract, current)


@pytest.mark.parametrize("name", ["id", "note"])
def test_removed_response_properties_including_optional(contract, name):
    current = deepcopy(contract)
    del output_schema(current)["properties"][name]
    assert any(
        f"properties/{name}" in change and "removed" in change
        for change in breaking_changes(contract, current)
    )


def test_response_required_removal(contract):
    current = deepcopy(contract)
    output_schema(current)["required"] = []
    assert any(
        "required response property 'id' removed" in change
        for change in breaking_changes(contract, current)
    )


@pytest.mark.parametrize("what", ["status", "media", "schema"])
def test_removed_documented_status_media_or_schema(contract, what):
    current = deepcopy(contract)
    if what == "status":
        del get(current)["responses"]["200"]
    elif what == "media":
        del get(current)["responses"]["200"]["content"]["application/json"]
    else:
        del response_media(current)["schema"]
    assert any("removed" in change for change in breaking_changes(contract, current))


@pytest.mark.parametrize("what", ["required", "removed", "type"])
def test_response_header_changes(contract, what):
    get(contract)["responses"]["200"]["headers"] = {
        "X-Version": {"required": True, "schema": {"type": "string"}}
    }
    current = deepcopy(contract)
    header = get(current)["responses"]["200"]["headers"]["X-Version"]
    if what == "required":
        header["required"] = False
    elif what == "removed":
        get(current)["responses"]["200"]["headers"] = {}
    else:
        header["schema"]["type"] = "integer"
    assert breaking_changes(contract, current)


@pytest.mark.parametrize(
    "mode", ["increase", "decrease", "scope", "global", "scheme", "remove_scheme"]
)
def test_authentication_and_security_scheme_changes(contract, mode):
    current = deepcopy(contract)
    if mode == "increase":
        get(contract)["security"] = []
    elif mode == "decrease":
        get(current)["security"] = []
    elif mode == "scope":
        get(current)["security"] = [{"Bearer": ["write"]}]
    elif mode == "global":
        contract["security"] = []
        current["security"] = [{"Bearer": []}]
        del get(contract)["security"]
        del get(current)["security"]
    elif mode == "scheme":
        current["components"]["securitySchemes"]["Bearer"]["scheme"] = "basic"
    else:
        current["components"]["securitySchemes"] = {}
    assert any(
        "security" in change or "authentication" in change
        for change in breaking_changes(contract, current)
    )


@pytest.mark.parametrize("level", ["root", "path", "operation"])
def test_explicit_server_prefix_changes_are_breaking(contract, level):
    current = deepcopy(contract)
    target = {
        "root": current,
        "path": current["paths"]["/items"],
        "operation": get(current),
    }[level]
    target["servers"] = [{"url": "/api"}]
    assert any(
        "servers/prefix" in change for change in breaking_changes(contract, current)
    )


@pytest.mark.parametrize(
    "key", ["ledger_file_bytes", "uvicorn_concurrency", "body_receive_seconds"]
)
def test_reduced_resource_limits(contract, key):
    current = deepcopy(contract)
    current["x-cashier-limits"][key] -= 1
    assert any(
        key in change and "decreased" in change
        for change in breaking_changes(contract, current)
    )


def test_nested_and_operation_limits(contract):
    current = deepcopy(contract)
    current["x-cashier-limits"]["nested"]["per_file_max"] -= 1
    assert any(
        "nested/per_file_max" in change
        for change in breaking_changes(contract, current)
    )
    get(contract)["x-cashier-limits"] = {"connections": 64}
    current = deepcopy(contract)
    get(current)["x-cashier-limits"]["connections"] = 32
    assert any(
        "GET /items/x-cashier-limits/connections" in change
        for change in breaking_changes(contract, current)
    )


def test_removed_limit_is_not_silently_accepted(contract):
    current = deepcopy(contract)
    del current["x-cashier-limits"]["ledger_file_bytes"]
    assert breaking_changes(contract, current)


def test_internal_refs_are_resolved_even_when_ref_text_is_unchanged(contract):
    current = deepcopy(contract)
    output_schema(current)["properties"]["id"]["type"] = "integer"
    assert any(
        "response type changed" in change
        for change in breaking_changes(contract, current)
    )
    current = deepcopy(contract)
    input_schema(current)["properties"]["count"]["maximum"] = 1
    assert any(
        "request maximum" in change for change in breaking_changes(contract, current)
    )


def test_renamed_and_escaped_local_refs_and_recursive_schemas(contract):
    output_schema(contract)["properties"]["next"] = {
        "$ref": "#/components/schemas/Output"
    }
    current = deepcopy(contract)
    schema = current["components"]["schemas"].pop("Output")
    current["components"]["schemas"]["Out/put~"] = schema
    ref = {"$ref": "#/components/schemas/Out~1put~0"}
    response_media(current)["schema"] = ref
    schema["properties"]["next"] = ref
    assert breaking_changes(contract, current) == []
    schema["properties"]["id"]["type"] = "integer"
    assert breaking_changes(contract, current)


@pytest.mark.parametrize("kind", ["parameter", "body", "response", "path"])
def test_non_schema_internal_refs_are_resolved(contract, kind):
    components = contract["components"]
    if kind == "parameter":
        components["parameters"] = {"Q": get(contract)["parameters"][0]}
        get(contract)["parameters"] = [{"$ref": "#/components/parameters/Q"}]
        current = deepcopy(contract)
        current["components"]["parameters"]["Q"]["required"] = True
    elif kind == "body":
        components["requestBodies"] = {"Body": post(contract)["requestBody"]}
        post(contract)["requestBody"] = {"$ref": "#/components/requestBodies/Body"}
        current = deepcopy(contract)
        current["components"]["requestBodies"]["Body"]["required"] = True
    elif kind == "response":
        components["responses"] = {"OK": get(contract)["responses"]["200"]}
        get(contract)["responses"]["200"] = {"$ref": "#/components/responses/OK"}
        current = deepcopy(contract)
        current["components"]["responses"]["OK"]["content"] = {}
    else:
        components["pathItems"] = {"Items": contract["paths"]["/items"]}
        contract["paths"]["/items"] = {"$ref": "#/components/pathItems/Items"}
        current = deepcopy(contract)
        del current["components"]["pathItems"]["Items"]["get"]
    assert breaking_changes(contract, contract) == []
    assert breaking_changes(contract, current)


@pytest.mark.parametrize(
    "ref",
    ["https://example.com/schema.json", "other.json#/Thing", "#missing", "#/missing"],
)
def test_unsupported_or_unresolved_refs_fail_closed_even_when_unchanged(contract, ref):
    response_media(contract)["schema"] = {"$ref": ref}
    assert any(
        "Cannot safely compare" in change
        for change in breaking_changes(contract, contract)
    )


def test_cyclic_aliases_and_semantic_ref_siblings_fail_closed(contract):
    contract["components"]["schemas"]["Loop"] = {"$ref": "#/components/schemas/Loop"}
    assert breaking_changes(contract, contract)
    del contract["components"]["schemas"]["Loop"]
    response_media(contract)["schema"]["maxProperties"] = 1
    assert breaking_changes(contract, contract)


@pytest.mark.parametrize("keyword", ["anyOf", "oneOf", "allOf"])
def test_complex_schemas_require_resolved_structural_equality(contract, keyword):
    response_media(contract)["schema"] = {
        keyword: [{"$ref": "#/components/schemas/Output"}, {"type": "null"}]
    }
    current = deepcopy(contract)
    assert breaking_changes(contract, current) == []
    output_schema(current)["properties"]["id"]["type"] = "integer"
    assert any(
        "complex response schema changed" in change
        for change in breaking_changes(contract, current)
    )


def test_nullable_and_unknown_schema_semantics_fail_closed_on_change(contract):
    output_schema(contract)["properties"]["note"]["nullable"] = True
    current = deepcopy(contract)
    output_schema(current)["properties"]["note"]["nullable"] = False
    assert breaking_changes(contract, current)
    current = deepcopy(contract)
    output_schema(current)["x-unknown-validation"] = {"rule": "new"}
    assert breaking_changes(contract, current)
    assert breaking_changes(current, deepcopy(current)) == []


def test_array_item_and_additional_property_schemas(contract):
    input_schema(contract)["properties"]["list"] = {
        "type": "array",
        "items": {"type": "integer", "minimum": 0},
    }
    current = deepcopy(contract)
    input_schema(current)["properties"]["list"]["items"]["minimum"] = 1
    assert breaking_changes(contract, current)
    current = deepcopy(contract)
    input_schema(current)["additionalProperties"] = False
    assert breaking_changes(contract, current)
    output_schema(contract)["additionalProperties"] = False
    current = deepcopy(contract)
    output_schema(current)["additionalProperties"] = True
    assert breaking_changes(contract, current)


@pytest.mark.parametrize("additional", [None, True, {}])
def test_added_optional_request_property_narrows_open_unknown_values(
    contract, additional
):
    if additional is not None:
        input_schema(contract)["additionalProperties"] = additional
    current = deepcopy(contract)
    input_schema(current)["properties"]["new"] = {"type": "string"}
    changes = breaking_changes(contract, current)
    assert any("properties/new" in change for change in changes), changes


def test_actual_v2_optional_request_property_rejects_previously_allowed_number():
    baseline = artifact("docs/api/openapi-v2.json")
    current = deepcopy(baseline)
    old_schema = baseline["components"]["schemas"]["XactRequest"]
    assert old_schema.get("additionalProperties", True) is True
    current["components"]["schemas"]["XactRequest"]["properties"]["client_note"] = {
        "type": "string"
    }
    formerly_valid = {"transactions": [], "client_note": 42}
    Draft202012Validator(old_schema).validate(formerly_valid)
    assert not Draft202012Validator(
        current["components"]["schemas"]["XactRequest"]
    ).is_valid(formerly_valid)
    changes = breaking_changes(baseline, current)
    assert any(
        "POST /xact/requestBody/" in change and "properties/client_note" in change
        for change in changes
    ), changes


def test_added_optional_response_property_breaks_closed_baseline(contract):
    output_schema(contract)["additionalProperties"] = False
    current = deepcopy(contract)
    output_schema(current)["properties"]["new"] = {"type": "string"}
    newly_valid = {"id": "123", "new": "extra"}
    Draft202012Validator(output_schema(current)).validate(newly_valid)
    assert not Draft202012Validator(output_schema(contract)).is_valid(newly_valid)
    changes = breaking_changes(contract, current)
    assert any("properties/new" in change for change in changes), changes


@pytest.mark.parametrize("direction", ["request", "response"])
@pytest.mark.parametrize(
    ("before", "after", "safe_directions"),
    [
        ({"type": "string"}, {"type": "string"}, {"request", "response"}),
        ({"type": "string"}, {"type": "integer"}, set()),
        (
            {"type": "integer", "minimum": 0},
            {"type": "integer", "minimum": 1},
            {"response"},
        ),
        (
            {"type": "integer", "minimum": 1},
            {"type": "integer", "minimum": 0},
            {"request"},
        ),
        ({"type": "string"}, {}, {"request"}),
    ],
)
def test_added_property_compares_typed_additional_properties(
    contract, direction, before, after, safe_directions
):
    select = input_schema if direction == "request" else output_schema
    select(contract)["additionalProperties"] = before
    current = deepcopy(contract)
    select(current)["properties"]["new"] = after
    changes = breaking_changes(contract, current)
    assert (changes == []) == (direction in safe_directions), changes


def test_added_optional_request_property_is_safe_when_baseline_was_closed(contract):
    input_schema(contract)["additionalProperties"] = False
    current = deepcopy(contract)
    input_schema(current)["properties"]["new"] = {"type": "string"}
    assert breaking_changes(contract, current) == []


@pytest.mark.parametrize("direction", ["request", "response"])
def test_new_additional_properties_does_not_apply_to_new_named_property(
    contract, direction
):
    select = input_schema if direction == "request" else output_schema
    select(contract)["additionalProperties"] = {"type": "string"}
    current = deepcopy(contract)
    select(current)["properties"]["new"] = {"type": "integer"}
    select(current)["additionalProperties"] = direction == "request"
    changes = breaking_changes(contract, current)
    assert any("properties/new" in change for change in changes), changes


@pytest.mark.parametrize("direction", ["request", "response"])
def test_added_property_resolves_typed_additional_properties_refs(contract, direction):
    select = input_schema if direction == "request" else output_schema
    contract["components"]["schemas"]["Extra"] = {"type": "string"}
    select(contract)["additionalProperties"] = {"$ref": "#/components/schemas/Extra"}
    current = deepcopy(contract)
    select(current)["properties"]["new"] = {"type": "integer"}
    assert breaking_changes(contract, current)
    select(current)["properties"]["new"] = {"type": "string"}
    assert breaking_changes(contract, current) == []


@pytest.mark.parametrize("direction", ["request", "response"])
@pytest.mark.parametrize("name", ["new", "unmatched"])
def test_added_property_with_pattern_properties_fails_closed(contract, direction, name):
    select = input_schema if direction == "request" else output_schema
    select(contract)["patternProperties"] = {"^new$": {"type": "string"}}
    select(contract)["additionalProperties"] = False
    current = deepcopy(contract)
    assert breaking_changes(contract, current) == []
    select(current)["properties"][name] = {"type": "string"}
    assert any(
        f"complex {direction} schema changed" in change
        for change in breaking_changes(contract, current)
    )


@pytest.mark.parametrize("additional", [None, True, {}])
def test_added_unrestricted_optional_request_property_is_safe(contract, additional):
    if additional is not None:
        input_schema(contract)["additionalProperties"] = additional
    current = deepcopy(contract)
    input_schema(current)["properties"]["new"] = {}
    assert breaking_changes(contract, current) == []


def test_path_parameters_merge_with_operation_overrides(contract):
    contract["paths"]["/items"]["parameters"] = [
        {"name": "q", "in": "query", "required": True, "schema": {"type": "string"}}
    ]
    get(contract)["parameters"][0]["required"] = False
    current = deepcopy(contract)
    get(current)["parameters"][0]["required"] = True
    assert breaking_changes(contract, current)
    current = deepcopy(contract)
    current["paths"]["/items"]["parameters"][0]["description"] = "Only docs changed"
    assert breaking_changes(contract, current) == []


def test_safe_additions_and_documentation_changes(contract):
    current = deepcopy(contract)
    current["info"]["version"] = "3.0.0"
    current["info"]["description"] = "Updated docs"
    current["servers"][0]["description"] = "Direct app URL"
    current["x-contract-version"] = "3.0.0"
    get(current).update(
        summary="New summary", description="New docs", operationId="renamed"
    )
    get(current)["parameters"][0]["example"] = "sample"
    response_media(current)["examples"] = {"example": {"value": {"id": "123"}}}
    input_schema(current)["properties"]["new"] = {}
    output_schema(current)["properties"]["new"] = {"type": "string"}
    get(current)["parameters"].append(
        {"name": "optional", "in": "query", "schema": {"type": "string"}}
    )
    current["paths"]["/new"] = {"get": {"responses": {"204": {"description": "OK"}}}}
    current["paths"]["/items"]["delete"] = {"responses": {"204": {"description": "OK"}}}
    current["x-cashier-limits"]["ledger_file_bytes"] *= 2
    assert breaking_changes(contract, current) == []


def test_request_widening_and_response_narrowing(contract):
    current = deepcopy(contract)
    input_schema(current)["required"] = []
    input_schema(current)["properties"]["kind"]["enum"].append("c")
    input_schema(current)["properties"]["count"].update(minimum=-1, maximum=101)
    output_schema(current)["properties"]["kind"]["enum"] = ["a"]
    output_schema(current)["properties"]["count"].update(minimum=1, maximum=99)
    output_schema(current)["required"].append("note")
    assert breaking_changes(contract, current) == []


def test_annotations_do_not_hide_properties_or_enum_literals(contract):
    input_schema(contract)["properties"]["description"] = {"type": "string"}
    input_schema(contract)["properties"]["payload"] = {"enum": [{"description": "a"}]}
    current = deepcopy(contract)
    input_schema(current)["properties"]["description"]["type"] = "integer"
    assert breaking_changes(contract, current)
    current = deepcopy(contract)
    input_schema(current)["properties"]["payload"]["enum"] = [{"description": "b"}]
    assert breaking_changes(contract, current)


@pytest.mark.parametrize(
    "keyword", ["default", "readOnly", "writeOnly", "discriminator", "x-validation"]
)
def test_unknown_changed_schema_keywords_are_not_ignored(contract, keyword):
    current = deepcopy(contract)
    input_schema(current)["properties"]["name"][keyword] = "changed"
    assert breaking_changes(contract, current)


@pytest.mark.parametrize("keyword", ["$id", "$dynamicRef", "$recursiveRef"])
def test_unsupported_reference_semantics_fail_closed(contract, keyword):
    output_schema(contract)[keyword] = "https://example.com/schema"
    assert breaking_changes(contract, contract)


def test_enum_removal_directions_and_json_boolean_distinction(contract):
    current = deepcopy(contract)
    del input_schema(current)["properties"]["kind"]["enum"]
    assert breaking_changes(contract, current) == []
    del output_schema(current)["properties"]["kind"]["enum"]
    assert breaking_changes(contract, current)
    input_schema(contract)["properties"]["kind"] = {"enum": [True]}
    current = deepcopy(contract)
    input_schema(current)["properties"]["kind"] = {"enum": [1]}
    assert breaking_changes(contract, current)


def test_unchanged_unknown_keywords_do_not_make_other_changes_safe(contract):
    input_schema(contract)["x-dependent-validation"] = {"rule": "uses properties"}
    current = deepcopy(contract)
    input_schema(current)["properties"]["new"] = {"type": "string"}
    assert breaking_changes(contract, current)
    assert breaking_changes(contract, deepcopy(contract)) == []


def test_alias_chains_resolve_to_changed_schema(contract):
    contract["components"]["schemas"]["Alias"] = {"$ref": "#/components/schemas/Output"}
    response_media(contract)["schema"] = {"$ref": "#/components/schemas/Alias"}
    current = deepcopy(contract)
    assert breaking_changes(contract, current) == []
    output_schema(current)["required"] = []
    assert any(
        "required response property" in change
        for change in breaking_changes(contract, current)
    )


@pytest.mark.parametrize("direction", ["request", "response"])
def test_new_array_item_schema_checks_direction(contract, direction):
    if direction == "request":
        input_schema(contract)["properties"]["list"] = {"type": "array"}
    else:
        output_schema(contract)["properties"]["list"] = {
            "type": "array",
            "items": {"type": "string"},
        }
    current = deepcopy(contract)
    if direction == "request":
        input_schema(current)["properties"]["list"]["items"] = {"type": "string"}
    else:
        del output_schema(current)["properties"]["list"]["items"]
    assert breaking_changes(contract, current)


def test_checker_is_deterministic_and_does_not_mutate_documents(contract):
    saved = deepcopy(contract)
    assert breaking_changes(contract, contract) == []
    assert contract == saved
    current = deepcopy(contract)
    output_schema(current)["required"] = []
    saved_current = deepcopy(current)
    first = breaking_changes(contract, current)
    assert first == breaking_changes(contract, current)
    assert current == saved_current
    assert contract == saved


@pytest.mark.parametrize("invalid", [{}, [], {"openapi": "2.0", "paths": {}}])
def test_invalid_or_unsupported_contracts_fail_closed(contract, invalid):
    assert breaking_changes(invalid, contract)
    assert breaking_changes(contract, invalid)


def run_cli(tmp_path, baseline, current):
    before, after = tmp_path / "baseline.json", tmp_path / "current.json"
    before.write_text(json.dumps(baseline), encoding="utf-8")
    after.write_text(json.dumps(current), encoding="utf-8")
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--baseline",
            str(before),
            "--current",
            str(after),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def test_cli_compatible_and_breaking_even_after_major_version_bump(contract, tmp_path):
    result = run_cli(tmp_path, contract, contract)
    assert result.returncode == 0, result.stderr
    assert "No breaking" in result.stdout
    current = deepcopy(contract)
    current["info"]["version"] = "3.0.0"
    current["x-contract-version"] = "3.0.0"
    del current["paths"]["/items"]["get"]
    result = run_cli(tmp_path, contract, current)
    assert result.returncode == 1
    assert "GET /items" in result.stderr


@pytest.mark.parametrize(
    "payload", ["{", '{"paths": {}, "paths": {}}', '{"value": NaN}']
)
def test_cli_invalid_json_is_nonzero(tmp_path, payload):
    path = tmp_path / "bad.json"
    path.write_text(payload, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--baseline", str(path), "--current", str(path)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 2
    assert "Cannot read API contracts" in result.stderr


def test_cli_missing_files_is_nonzero(tmp_path):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--baseline", str(tmp_path / "missing.json")],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 2


def test_cli_defaults_are_repo_relative_not_working_directory(
    contract, tmp_path, monkeypatch, capsys
):
    default_dir = tmp_path / "docs/api/baselines"
    default_dir.mkdir(parents=True)
    (default_dir / "openapi-v2.json").write_text(json.dumps(contract), encoding="utf-8")
    (default_dir.parent / "openapi-v2.json").write_text(
        json.dumps(contract), encoding="utf-8"
    )
    monkeypatch.setattr(checker, "ROOT", tmp_path)
    assert checker.main([]) == 0
    assert "No breaking" in capsys.readouterr().out


def artifact(relative):
    path = ROOT / relative
    assert path.is_file(), f"Missing committed contract artifact: {relative}"
    return json.loads(path.read_text(encoding="utf-8"))


def test_committed_v2_baseline_is_compatible():
    baseline = artifact("docs/api/baselines/openapi-v2.json")
    current = artifact("docs/api/openapi-v2.json")
    assert breaking_changes(baseline, current) == []
    assert (
        baseline["x-contract-version"].split(".")[0]
        == current["x-contract-version"].split(".")[0]
    )


def test_migration_record_pins_frozen_baselines_and_explicit_breaks():
    record = artifact("docs/api/baselines/migration-v1-v2.json")
    old = artifact("docs/api/baselines/openapi-v1.json")
    new = artifact("docs/api/baselines/openapi-v2.json")
    assert record["from_contract_version"] == old["x-contract-version"] == "1.0.0"
    assert record["to_contract_version"] == new["x-contract-version"] == "2.0.0"
    assert int(new["x-contract-version"].split(".")[0]) > int(
        old["x-contract-version"].split(".")[0]
    )
    assert record["reason"].strip()
    assert set(record["baseline_sha256"]) == {"openapi-v1.json", "openapi-v2.json"}
    for name, digest in record["baseline_sha256"].items():
        assert (
            hashlib.sha256(
                (ROOT / "docs/api/baselines" / name).read_bytes()
            ).hexdigest()
            == digest
        )
    changes = breaking_changes(old, new)
    assert changes
    assert record["accepted_breaking_changes"] == changes


@pytest.mark.parametrize("missing", ["openapi-v1.json", "openapi-v2.json", "both"])
def test_migration_record_requires_both_baseline_pins(monkeypatch, missing):
    record = deepcopy(artifact("docs/api/baselines/migration-v1-v2.json"))
    if missing == "both":
        record["baseline_sha256"] = {}
    else:
        del record["baseline_sha256"][missing]
    original_artifact = artifact

    def altered_artifact(relative):
        if relative == "docs/api/baselines/migration-v1-v2.json":
            return record
        return original_artifact(relative)

    monkeypatch.setitem(globals(), "artifact", altered_artifact)
    with pytest.raises(AssertionError):
        test_migration_record_pins_frozen_baselines_and_explicit_breaks()


def test_actual_v1_to_v2_intentional_breaks_are_reported():
    baseline = artifact("docs/api/baselines/openapi-v1.json")
    current = artifact("docs/api/openapi-v2.json")
    changes = breaking_changes(baseline, current)
    assert any(
        change.startswith("GET /:") and "removed" in change for change in changes
    ), changes
    assert any(
        change.startswith("GET /reload:") and "removed" in change for change in changes
    ), changes
    assert any(
        change.startswith("GET /health/")
        and "beancount_loaded" in change
        and "removed" in change
        for change in changes
    ), changes
