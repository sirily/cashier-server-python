# Cashier server HTTP contract v2

[`openapi-v2.json`](api/openapi-v2.json) is the current version-controlled OpenAPI 3.1 contract. `x-contract-version` is **2.0.0**, intentionally incompatible with v1. `info.version` is the server release version; it is not the contract version and is selected separately by the repository owner.


## Paths, deployment and authorization

| Operation | Server path | Authorization |
| --- | --- | --- |
| Root snapshot | `GET /infrastructure?file_path=main.bean` | Bearer |
| Manual writeback | `POST /xact` | Bearer |
| Health | `GET /health` | Public |
| Ping | `GET /ping` | Bearer |

OpenAPI `servers: [{"url":"/"}]` describes server-relative addressing. A reverse proxy may expose `/xact` as `/api/xact`, but that prefix, any rewriting, TLS termination and proxy body/connection/time limits must be checked in the actual deployment. No `/api/...` routes are registered by the app. Proxy limits may reject requests before the server and are not promises made by the server's OpenAPI. See [Deployment](deployment.md) for mounting the ledger and serving the PWA and API on the same HTTPS origin.

Send `Authorization: Bearer <CASHIER_API_TOKEN>`. Missing/invalid tokens return `401` and `WWW-Authenticate: Bearer`; missing server token configuration fails closed with `503`. The middleware also protects unknown routes and `OPTIONS`; authenticate when checking that a removed route returns `404`. Health is exempt from bearer authentication, not from Host or request-limit checks.

`/ping` returns JSON `"pong"`. `/health` returns `{"ok":true,"beancount_file_configured":true}` for a configured running process. It does not read files, execute plugins or promise that the ledger can currently be materialized. The entry point requires `BEANCOUNT_FILE` and the bearer token but does not preload ledger content.

## Root snapshot and current-file guarantees

`file_path` is required and identifies only the configured `BEANCOUNT_FILE` basename (use its actual name if not `main.bean`). The response is `{"content":"..."}`. It is a source-preserving standalone ledger: includes/globs expand on the server, permitted Python plugin results are materialized, and the client receives no active include or Python plugin directives. It is not a raw file-download API. Included files, the manual file when it is not the configured root, unrelated paths and all HTTP glob patterns are rejected.

Every snapshot request captures the **current source files** in a new private directory outside the ledger tree. An external change to an included file or addition under a glob is visible on the next successful pull. No completed snapshot or BQL cache is reused. A read, parse, booking or materialization failure returns an error (normally `422` with `detail.message` and `detail.errors`), never old content or raw-root fallback as a successful response. After repairing the source, the next request builds from the repaired files.

Capture uses descriptor-relative no-follow reads and rejects unsafe includes, symlinks and unsupported auxiliary/workspace Python imports. Source capture is bounded but is not an atomic filesystem-wide snapshot against unrelated writers. External writers must coordinate; plugins remain trusted installed server code, not sandboxed code.

## Writeback guarantees beyond JSON Schema

`POST /xact` receives `{"transactions":["...Beancount text..."]}`. Only completed `*` Cashier transactions with valid stable `cashier_id` and already-known accounts are accepted. It is not an account-creation or general ledger-mutation API. Other directives, incomplete transactions, unknown accounts and malformed/unbalanced transactions are rejected with concrete per-item reasons.

- **Idempotency:** same `cashier_id` plus equivalent normalized content returns the ID in `synchronized` without appending. Whitespace/metadata ordering is not transaction identity. Changed content under an existing ID is a conflict in `rejected`; never an automatic overwrite. IDs are case-sensitive and must be retained for retries.
- **Partial success:** HTTP 200 can contain both `synchronized` and `rejected`. Successful HTTP status is not confirmation of every item. An unrecoverable ID is represented as `cashier_id: null`. Full-pipeline failure can reject all pending new items; equivalent existing IDs may already be synchronized. Conflicts are per-item results, not HTTP 409 responses.
- **Exact total prices:** `@@ 1.00 USD` stays that exact total in the manual file and root snapshot, never a rounded `@` unit price. Explicit `@` stays a unit price. Different total, currency or notation is a conflict even if rounded parsed unit prices compare equal. Historical lossy entries are not automatically repaired.
- **Fresh validation:** writeback captures and validates current authoritative files with the uncached loader under the manual-file lock. Externally added/removed accounts affect the next validation. Invalid or unreadable current books fail closed, without appending based on stale account/ledger state.
- **Single writable file:** the target comes only from `BEANCOUNT_MANUAL_TRANSACTIONS_FILE`; clients cannot supply it. Under a cross-process file lock the server rereads, deduplicates, builds a private candidate, validates it through the configured full root and durably updates only the mounted manual file. No writable ledger directory or workspace temp file is required. This is crash-conscious, not crash-atomic. Recovery markers remain intact and incomplete/corrupted batches fail closed.
- **Confirmation:** successful new IDs are returned after durable append. There is no cache refresh after commit. Pull the full root snapshot and confirm synchronized IDs; a subsequent snapshot failure is an error, not a retraction of a durable write or permission to reuse old content.

## Limits and errors

`x-cashier-limits` records server limits that cannot all be represented by JSON Schema; UTF-8 byte limits are not character counts.

- HTTP: 1 MiB request body, 64 KiB query string, 15-second body receive deadline. Invalid Host/Content-Length returns `400`; size/deadline checks may run before auth/JSON parsing (`413`/`408`).
- Admission: four shared heavy-operation slots per process for snapshot/writeback; the built-in Uvicorn entry point limits concurrency to 64. Application overload is JSON `503`; Uvicorn may return plain-text `503` before any route, including health. These are not proxy settings.
- Writeback: 100 items per request; each at most 64 KiB UTF-8, 256 postings and 64 metadata fields. Metadata keys are at most 64 characters; string values at most 1024 UTF-8 bytes. `cashier_id` is 1–128 ASCII characters, starting with a letter/digit, followed by letters/digits or `._:-`. Shape/batch validation is HTTP `422`; item limits produce per-item rejections at HTTP 200.
- Snapshot path: at most 1024 characters, no controls/absolute/hidden/traversal paths; only the configured root basename is exposed.
- Ledger capture: 16 MiB/file, 64 MiB captured/expanded input, 4096 files/include visits and 64 include levels. Directory scans/glob expansion also retain their existing finite budgets. Capture and pipeline errors fail closed.

Missing configuration/filesystem/writeback pipeline failures can return `500` with string detail. FastAPI validation uses HTTP `422` with a detail array; snapshot build failures use a detail object. See the status-specific schemas in OpenAPI.

## Intentional v1 → v2 migration

- Removed `GET /?query=...` and `GET /reload`. Authenticated requests return `404`; the server has no BQL executor or post-write cache refresh.
- Removed `beancount_loaded` from health. Health reports the running process and whether a root is configured; it is not a ledger readiness probe.
- OpenAPI lists server-relative paths only. The external `/api` prefix and proxy limits belong to deployment configuration.
- Clients must pull the standalone root snapshot, parse it locally, and derive accounts, payees, and balances there. After writeback, pull again and confirm `cashier_id`; no reload call is needed.

The frozen [v1 baseline](api/baselines/openapi-v1.json) is retained for migration analysis, not as supported API documentation. [migration-v1-v2.json](api/baselines/migration-v1-v2.json) records the intentional incompatibilities, contract versions, reason, and hashes of both frozen baselines.

## Compatibility gate and baseline updates

The metadata in `main.py` and `cashier_snapshot/api_contract.py` generates the saved artifact. Tests compare `app.openapi()` with it, validate schemas/references and real HTTP responses. `scripts/check_api_contract.py` compares the current artifact with the frozen same-major baseline; CI runs it explicitly and runs its regression tests in pytest.

```sh
uv run --locked python scripts/check_api_contract.py
uv run --locked pytest tests/test_api_contract.py tests/test_api_compatibility.py tests/test_snapshot_freshness.py
```

The checker detects removal of paths/methods, added required inputs, narrowed request schemas, incompatible response schemas/removed properties, lost statuses/media/headers, changed auth/server prefixes and reduced server limits. It resolves local references. It is a **conservative supported-subset checker**, not a complete JSON Schema inclusion proof: complex/unknown semantic changes fail closed, external refs are unsupported, and it cannot establish idempotency, exact prices or plugin/file semantics. Those require the behavioral regressions.

Do not refresh a baseline just to make a failing check pass. For compatible edits, leave the baseline unchanged and review the generated artifact diff. For an intentional break:

1. Review the reported incompatibilities and client migration requirements.
2. Increment the **contract major**, not the owner-selected server release version automatically.
3. Preserve the previous baseline byte-for-byte. Generate the new versioned artifact, then explicitly establish its new major baseline.
4. Record old/new versions, baseline hashes, reason and exact compatibility diagnostics in a new migration record; test that the old→new comparison still reports the intended break.
5. Change the default baseline/artifact for the new major, document migration, regenerate the schema, and run both compatibility and HTTP/semantic tests. A version bump by itself never suppresses checker failures.

Generate the current artifact from the repository root after intentional metadata changes:

```sh
uv run --locked python -c 'import json; from pathlib import Path; import main; Path("docs/api/openapi-v2.json").write_text(json.dumps(main.app.openapi(), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")'
```

No baseline update or successful schema check is a substitute for snapshot freshness, file-boundary, idempotency, partial-success, plugin and RustLedger tests.
