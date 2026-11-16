"""Conservative, stdlib-only OpenAPI compatibility gate for committed contracts.

This is deliberately not a general OpenAPI/JSON Schema subsumption solver. Simple
request schemas may widen and response schemas may narrow; optional properties
may be added when compatible with the baseline's additionalProperties policy,
and operations may be added. Complex schemas must remain structurally equal
(after resolving local refs and ignoring annotations). Unknown semantic changes
fail closed. External refs and semantic $ref siblings are unsupported, even when
unchanged. Servers (including explicit proxy prefixes), auth, and security scheme
changes are breaking. Contract/version bumps never waive a reported break: the
committed baseline must be updated explicitly after reviewing a migration.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
METHODS = frozenset(
    {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
)
ANNOTATIONS = frozenset(
    {
        "description",
        "summary",
        "title",
        "example",
        "examples",
        "operationId",
        "externalDocs",
    }
)
# These objects contain user-defined names, not OpenAPI/Schema keyword names.
NAME_MAPS = frozenset(
    {
        "properties",
        "patternProperties",
        "$defs",
        "definitions",
        "schemas",
        "paths",
        "responses",
        "content",
        "headers",
        "securitySchemes",
        "parameters",
        "requestBodies",
        "pathItems",
        "dependentRequired",
        "mapping",
        "callbacks",
        "links",
        "variables",
        "encoding",
        "dependentSchemas",
    }
)
LITERAL_VALUES = frozenset({"enum", "const", "default", "security", "x-cashier-limits"})
LOWER_BOUNDS = frozenset(
    {
        "minimum",
        "exclusiveMinimum",
        "minLength",
        "minItems",
        "minProperties",
        "minContains",
    }
)
UPPER_BOUNDS = frozenset(
    {
        "maximum",
        "exclusiveMaximum",
        "maxLength",
        "maxItems",
        "maxProperties",
        "maxContains",
    }
)


class UnsupportedContract(ValueError):
    """A contract cannot be compared safely by this checker."""


def _semantic(value, names=False):
    if isinstance(value, list):
        return [_semantic(item) for item in value]
    if not isinstance(value, dict):
        return value
    if names:
        return {key: _semantic(item) for key, item in value.items()}
    return {
        key: item if key in LITERAL_VALUES else _semantic(item, key in NAME_MAPS)
        for key, item in value.items()
        if key not in ANNOTATIONS
    }


def _resolve(node: Any, document: dict) -> Any:
    seen = set()
    while isinstance(node, dict) and "$ref" in node:
        ref = node["$ref"]
        if not isinstance(ref, str) or not (ref == "#" or ref.startswith("#/")):
            raise UnsupportedContract(f"unsupported external/non-pointer $ref: {ref!r}")
        if ref in seen:
            raise UnsupportedContract(f"cyclic reference alias: {ref}")
        seen.add(ref)
        if set(node) != {"$ref"}:
            raise UnsupportedContract(f"unsupported semantic $ref siblings: {ref}")
        node = document
        try:
            for token in ref[2:].split("/") if ref != "#" else []:
                token = token.replace("~1", "/").replace("~0", "~")
                node = node[int(token)] if isinstance(node, list) else node[token]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise UnsupportedContract(f"unresolved $ref: {ref}") from exc
        if not isinstance(node, (dict, bool)):
            raise UnsupportedContract(f"$ref does not target an object/schema: {ref}")
    return node


def _validate_refs(node, document, location="contract", names=False):
    if isinstance(node, dict):
        if not names:
            for keyword in ("$dynamicRef", "$recursiveRef", "$id"):
                if keyword in node:
                    raise UnsupportedContract(f"{location}: unsupported {keyword}")
            if "$ref" in node:
                _resolve(node, document)
        for key, value in node.items():
            if names or key not in LITERAL_VALUES:
                _validate_refs(
                    value, document, f"{location}/{key}", not names and key in NAME_MAPS
                )
    elif isinstance(node, list):
        for value in node:
            _validate_refs(value, document, location)


class _Comparison:
    def __init__(self, baseline, current):
        self.old = baseline
        self.new = current
        self.changes: list[str] = []

    def fail(self, location, reason):
        self.changes.append(f"{location}: {reason}")

    def equal(self, old, new, active=None):
        """Structural equality with local refs, including recursive schemas."""
        old, new = _resolve(old, self.old), _resolve(new, self.new)
        if type(old) is not type(new):
            return False
        if not isinstance(old, (dict, list)):
            return old == new
        active = set() if active is None else active
        pair = (id(old), id(new))
        if pair in active:
            return True
        active = active | {pair}
        if isinstance(old, list):
            return len(old) == len(new) and all(
                self.equal(a, b, active) for a, b in zip(old, new)
            )
        return old.keys() == new.keys() and all(
            self.equal(value, new[key], active) for key, value in old.items()
        )

    def extras(self, old, new, handled, location):
        for key in sorted((old.keys() | new.keys()) - handled):
            if key not in old or key not in new or not self.equal(old[key], new[key]):
                self.fail(location, f"unhandled semantic field {key!r} changed")

    def schema(self, old, new, direction, location, active=None):
        old, new = _resolve(old, self.old), _resolve(new, self.new)
        if not isinstance(old, (dict, bool)) or not isinstance(new, (dict, bool)):
            self.fail(location, "unsupported schema (expected object or boolean)")
            return
        # Both forms accept every JSON value, including unknown property values.
        old = True if old == {} else old
        new = True if new == {} else new
        if isinstance(old, bool) or isinstance(new, bool):
            if old == new:
                return
            safe = (
                (old is False or new is True)
                if direction == "request"
                else (new is False or old is True)
            )
            if not safe:
                self.fail(
                    location,
                    f"{direction} boolean schema narrowed/widened incompatibly",
                )
            return
        active = set() if active is None else active
        pair = (id(old), id(new), direction)
        if pair in active:
            return
        active = active | {pair}
        simple_keywords = {
            "type",
            "required",
            "properties",
            "enum",
            "items",
            "additionalProperties",
            *LOWER_BOUNDS,
            *UPPER_BOUNDS,
        }
        if (old.keys() | new.keys()) - simple_keywords:
            if not self.equal(old, new):
                self.fail(
                    location,
                    f"complex {direction} schema changed; compatibility unproven",
                )
            return

        if old.get("type") != new.get("type"):
            self.fail(
                location,
                f"{direction} type changed: {old.get('type')!r} -> {new.get('type')!r}",
            )
        old_required, new_required = (
            set(old.get("required", [])),
            set(new.get("required", [])),
        )
        lost = (
            new_required - old_required
            if direction == "request"
            else old_required - new_required
        )
        for name in sorted(lost):
            self.fail(
                location,
                f"required {direction} property {name!r} "
                + ("added" if direction == "request" else "removed"),
            )
        old_props, new_props = old.get("properties", {}), new.get("properties", {})
        for name in sorted(old_props):
            where = f"{location}/properties/{name}"
            if name not in new_props:
                self.fail(
                    where,
                    f"{direction} property removed (including optional properties)",
                )
            else:
                self.schema(old_props[name], new_props[name], direction, where, active)
        for name in sorted(new_props.keys() - old_props.keys()):
            # Naming a property exempts it from additionalProperties. Requests
            # must still accept its former values; responses must only produce
            # values the baseline allowed. Pattern schemas fail closed above.
            self.schema(
                old.get("additionalProperties", True),
                new_props[name],
                direction,
                f"{location}/properties/{name}",
                active,
            )

        if "enum" in old or "enum" in new:
            # JSON serialization distinguishes JSON true from the number 1.
            old_enum = {json.dumps(v, sort_keys=True) for v in old.get("enum", [])}
            new_enum = {json.dumps(v, sort_keys=True) for v in new.get("enum", [])}
            safe = (
                "enum" not in new or ("enum" in old and old_enum <= new_enum)
                if direction == "request"
                else "enum" not in old or ("enum" in new and new_enum <= old_enum)
            )
            if not safe:
                self.fail(location, f"{direction} enum changed incompatibly")

        for key in sorted(LOWER_BOUNDS | UPPER_BOUNDS):
            before, after = old.get(key), new.get(key)
            if before == after:
                continue
            if any(
                v is not None and (type(v) not in (int, float) or not math.isfinite(v))
                for v in (before, after)
            ):
                self.fail(location, f"unsupported {key} constraint change")
                continue
            # Acceptance expands when a lower bound falls or an upper bound rises.
            if direction == "request":
                safe = after is None or (
                    before is not None
                    and (after <= before if key in LOWER_BOUNDS else after >= before)
                )
            else:
                safe = before is None or (
                    after is not None
                    and (after >= before if key in LOWER_BOUNDS else after <= before)
                )
            if not safe:
                self.fail(
                    location,
                    f"{direction} {key} changed incompatibly: {before!r} -> {after!r}",
                )

        for key in ("items", "additionalProperties"):
            if key in old or key in new:
                default = True if key == "additionalProperties" else {}
                self.schema(
                    old.get(key, default),
                    new.get(key, default),
                    direction,
                    f"{location}/{key}",
                    active,
                )
        self.extras(
            old,
            new,
            {
                "type",
                "required",
                "properties",
                "enum",
                "items",
                "additionalProperties",
                *LOWER_BOUNDS,
                *UPPER_BOUNDS,
            },
            location,
        )

    def limits(self, old, new, location):
        if isinstance(old, dict) and isinstance(new, dict):
            for key in sorted(old.keys() | new.keys()):
                where = f"{location}/{key}"
                if key not in old or key not in new:
                    self.fail(where, "limit added/removed; compatibility unproven")
                else:
                    self.limits(old[key], new[key], where)
        elif (
            isinstance(old, (int, float))
            and not isinstance(old, bool)
            and isinstance(new, (int, float))
            and not isinstance(new, bool)
        ):
            if not math.isfinite(old) or not math.isfinite(new) or new < old:
                self.fail(location, f"limit decreased or invalid: {old!r} -> {new!r}")
        elif not self.equal(old, new):
            self.fail(location, "unknown limit semantics changed")

    def content(self, old, new, direction, location):
        for media, before in sorted(old.items()):
            where = f"{location}/{media}"
            if media not in new:
                self.fail(where, f"documented {direction} media type removed")
                continue
            after = new[media]
            if "schema" in before and "schema" not in after:
                self.fail(where, f"documented {direction} media schema removed")
            else:
                self.schema(
                    before.get("schema", {}), after.get("schema", {}), direction, where
                )
            self.extras(before, after, {"schema"}, where)

    def parameter(self, old, new, direction, location):
        old, new = _resolve(old, self.old), _resolve(new, self.new)
        before, after = old.get("required", False), new.get("required", False)
        if (direction == "request" and after and not before) or (
            direction == "response" and before and not after
        ):
            self.fail(
                location, f"required {direction} parameter/header changed incompatibly"
            )
        if "schema" in old and "schema" not in new:
            self.fail(location, "documented parameter/header schema removed")
        else:
            self.schema(
                old.get("schema", {}), new.get("schema", {}), direction, location
            )
        self.content(
            old.get("content", {}), new.get("content", {}), direction, location
        )
        self.extras(old, new, {"schema", "content", "required"}, location)

    def parameters(self, path_item, operation, document):
        result = {}
        for parameter in [
            *path_item.get("parameters", []),
            *operation.get("parameters", []),
        ]:
            parameter = _resolve(parameter, document)
            result[(parameter["in"], parameter["name"])] = parameter
        return result

    def operation(self, path_old, path_new, old, new, location):
        old_params = self.parameters(path_old, old, self.old)
        new_params = self.parameters(path_new, new, self.new)
        for key, before in sorted(old_params.items()):
            where = f"{location}/parameters/{key[0]}/{key[1]}"
            if key not in new_params:
                self.fail(where, "request parameter removed")
            else:
                self.parameter(before, new_params[key], "request", where)
        for key in sorted(new_params.keys() - old_params.keys()):
            if new_params[key].get("required", False):
                self.fail(location, f"required request parameter {key!r} added")

        before = _resolve(old.get("requestBody", {}), self.old)
        after = _resolve(new.get("requestBody", {}), self.new)
        if after.get("required", False) and not before.get("required", False):
            self.fail(location, "required request body added")
        if "requestBody" in old and "requestBody" not in new:
            self.fail(location, "request body removed")
        self.content(
            before.get("content", {}),
            after.get("content", {}),
            "request",
            f"{location}/requestBody",
        )
        self.extras(before, after, {"required", "content"}, f"{location}/requestBody")

        responses = new.get("responses", {})
        for status, response in sorted(old.get("responses", {}).items()):
            where = f"{location}/responses/{status}"
            if status not in responses:
                self.fail(where, "documented response status removed")
                continue
            response, current = (
                _resolve(response, self.old),
                _resolve(responses[status], self.new),
            )
            self.content(
                response.get("content", {}),
                current.get("content", {}),
                "response",
                where,
            )
            for name, header in sorted(response.get("headers", {}).items()):
                if name not in current.get("headers", {}):
                    self.fail(
                        f"{where}/headers/{name}", "documented response header removed"
                    )
                else:
                    self.parameter(
                        header,
                        current["headers"][name],
                        "response",
                        f"{where}/headers/{name}",
                    )
            self.extras(response, current, {"content", "headers"}, where)

        def security(operation, document):
            requirements = operation.get("security", document.get("security", []))
            return sorted(
                json.dumps({k: sorted(v) for k, v in entry.items()}, sort_keys=True)
                for entry in requirements
            )

        if security(old, self.old) != security(new, self.new):
            self.fail(location, "authentication/security requirements changed")
        before_servers = old.get(
            "servers", path_old.get("servers", self.old.get("servers", [{"url": "/"}]))
        )
        after_servers = new.get(
            "servers", path_new.get("servers", self.new.get("servers", [{"url": "/"}]))
        )
        if not self.equal(before_servers, after_servers):
            self.fail(location, "effective servers/prefix changed")
        self.limits(
            old.get("x-cashier-limits", {}),
            new.get("x-cashier-limits", {}),
            f"{location}/x-cashier-limits",
        )
        self.extras(
            old,
            new,
            {
                "parameters",
                "requestBody",
                "responses",
                "security",
                "servers",
                "x-cashier-limits",
            },
            location,
        )

    def run(self):
        if self.old.get("openapi") != self.new.get("openapi"):
            self.fail(
                "contract", "OpenAPI dialect/version changed; compatibility unproven"
            )
        if not self.equal(
            self.old.get("servers", [{"url": "/"}]),
            self.new.get("servers", [{"url": "/"}]),
        ):
            self.fail("contract", "servers/prefix changed")
        old_schemes = self.old.get("components", {}).get("securitySchemes", {})
        new_schemes = self.new.get("components", {}).get("securitySchemes", {})
        for name, scheme in sorted(old_schemes.items()):
            if name not in new_schemes or not self.equal(scheme, new_schemes[name]):
                self.fail(
                    f"securitySchemes/{name}", "security scheme removed or changed"
                )
        if not self.equal(self.old.get("security", []), self.new.get("security", [])):
            self.fail("contract", "global authentication/security requirements changed")
        self.limits(
            self.old.get("x-cashier-limits", {}),
            self.new.get("x-cashier-limits", {}),
            "x-cashier-limits",
        )
        for path, path_old in sorted(self.old["paths"].items()):
            path_old = _resolve(path_old, self.old)
            path_new = _resolve(self.new["paths"].get(path, {}), self.new)
            for method in sorted(METHODS & path_old.keys()):
                location = f"{method.upper()} {path}"
                if method not in path_new:
                    self.fail(
                        location,
                        "operation removed"
                        if path in self.new["paths"]
                        else "path removed",
                    )
                else:
                    self.operation(
                        path_old, path_new, path_old[method], path_new[method], location
                    )
            if path in self.new["paths"]:
                self.limits(
                    path_old.get("x-cashier-limits", {}),
                    path_new.get("x-cashier-limits", {}),
                    f"{path}/x-cashier-limits",
                )
                self.extras(
                    path_old,
                    path_new,
                    {*METHODS, "parameters", "servers", "x-cashier-limits"},
                    path,
                )
        self.extras(
            self.old,
            self.new,
            {
                "openapi",
                "info",
                "paths",
                "components",
                "servers",
                "security",
                "x-cashier-limits",
                "x-contract-version",
            },
            "contract",
        )
        self.extras(
            self.old.get("components", {}),
            self.new.get("components", {}),
            {
                "schemas",
                "securitySchemes",
                "parameters",
                "requestBodies",
                "responses",
                "headers",
            },
            "components",
        )
        return self.changes


def breaking_changes(baseline: dict, current: dict) -> list[str]:
    """Return deterministic incompatibility diagnostics without modifying inputs.

    Invalid/unresolvable/unsupported references are diagnostics, not a successful
    comparison. Unchanged unknown keywords are accepted structurally, not proven
    semantically. Only local JSON pointers are resolved; no network access occurs.
    """
    try:
        documents = []
        for label, raw in (("baseline", baseline), ("current", current)):
            if not isinstance(raw, dict) or not isinstance(raw.get("paths"), dict):
                raise UnsupportedContract(
                    f"{label}: expected an OpenAPI object with paths"
                )
            if not str(raw.get("openapi", "")).startswith(("3.0.", "3.1.")):
                raise UnsupportedContract(f"{label}: unsupported OpenAPI version")
            document = _semantic(raw)
            _validate_refs(document, document, label)
            documents.append(document)
        return _Comparison(*documents).run()
    except (
        UnsupportedContract,
        KeyError,
        TypeError,
        ValueError,
        AttributeError,
        RecursionError,
    ) as exc:
        return [f"Cannot safely compare contracts: {exc}"]


def _load(path):
    def invalid_constant(value):
        raise ValueError(f"invalid JSON numeric constant: {value}")

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    with path.open(encoding="utf-8") as stream:
        return json.load(
            stream, parse_constant=invalid_constant, object_pairs_hook=unique_object
        )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--baseline", type=Path, default=ROOT / "docs/api/baselines/openapi-v2.json"
    )
    parser.add_argument(
        "--current", type=Path, default=ROOT / "docs/api/openapi-v2.json"
    )
    args = parser.parse_args(argv)
    try:
        changes = breaking_changes(_load(args.baseline), _load(args.current))
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        print(f"Cannot read API contracts: {exc}", file=sys.stderr)
        return 2
    if changes:
        print("Breaking or unproven API compatibility changes:", file=sys.stderr)
        for change in changes:
            print(f"- {change}", file=sys.stderr)
        return 1
    print("No breaking API contract changes detected by the conservative checker.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
