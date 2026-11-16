# Cashier server HTTP contract v1

[`openapi-v1.json`](openapi-v1.json) is the version-controlled OpenAPI 3.1 contract. `x-contract-version` is `1.0.0`; `info.version` is the server release version, not an independent choice of release number. Compatible additions can update the v1 contract; incompatible changes require an explicit compatibility decision and a new contract major rather than silently changing an accepted Sync request.

The artifact is generated from FastAPI route metadata in `main.py` and `cashier_snapshot/api_contract.py`. Contract tests compare it with `app.openapi()`, validate its schemas and references, and validate real HTTP responses against those schemas. `jsonschema` is a development-only dependency.

## Paths, transport and authorization

Paths in the artifact are **server-relative**, without `/api`:

| Operation | Server path | External deployment path | Authorization |
| --- | --- | --- | --- |
| BQL | `GET /?query=...` | `GET /api/?query=...` | Bearer |
| Root snapshot | `GET /infrastructure?file_path=main.bean` | `GET /api/infrastructure?file_path=main.bean` | Bearer |
| Manual writeback | `POST /xact` | `POST /api/xact` | Bearer |
| Health | `GET /health` | `GET /api/health` if forwarded | Public |
| Ping | `GET /ping` | `GET /api/ping` | Bearer |
| Reload | `GET /reload` | `GET /api/reload` | Bearer |

OpenAPI `servers` lists `/` for direct server requests and `/api` for the deployed prefix. The proxy strips the external prefix; the app does not register duplicate `/api/...` routes. HTTPS terminates outside the server; Caddy forwards internal HTTP. PWA and API are same-origin. There is no server CORS or direct TLS mode.

Send `Authorization: Bearer <CASHIER_API_TOKEN>`. Missing/invalid tokens return `401` and `WWW-Authenticate: Bearer`; missing server token configuration fails closed with `503`. Health does not require a token, but still undergoes Host and request-limit validation. Health reports startup state, not a fresh validation of the ledger. These rules also apply to `OPTIONS`; preflight does not bypass authentication. Do not use `servers` to infer a publicly reachable internal port.

## BQL compatibility and response semantics

Supported Sync requests include:

```sql
SELECT account FROM #accounts WHERE close IS NULL ORDER BY account
SELECT DISTINCT COALESCE(payee, narration) AS payee FROM transactions ORDER BY payee
SELECT sum(number), currency, account ORDER BY account
```

The third request is a narrow compatibility exception: the **whole top-level AST** must match that shape. Whitespace, keyword case, default/explicit ascending order and BQL comments may differ without changing the AST. It is bound to the trusted Beancount **postings** table, preserving native Beanquery implicit grouping by currency and account. `accounts` is not equivalent: it lists account declarations and cannot supply posting numbers/currencies. The result is a JSON array of `[sum(number), currency, account]` rows ordered by account. Order among currencies within the same account is unspecified. Open accounts with no postings do not gain synthetic zero rows. Numbers retain the existing Decimal-to-JSON-number encoding, not string encoding; JSON numeric precision is distinct from exact Beancount text preservation.

Other SELECTs, including nested SELECTs, require an explicit `accounts` or `transactions` source; `#accounts` and `#transactions` are accepted. Subqueries must satisfy the same policy. Explicit `postings`, arbitrary implicit SELECTs, wrapping/extending the compatibility query, other tables, external sources, mutation statements, `BALANCES`, `JOURNAL`, `PRINT`, expression-based FROM and OPEN/CLOSE/CLEAR are not restored. AST validation is completed with resource bounds before source canonicalization. Workers receive trusted entries/options only, not parent tables, functions or filenames.

BQL results are row arrays, not column-keyed objects. Missing or empty `query` preserves the legacy HTTP 200 `{"error":"No query provided"}` envelope. Syntax/policy/resource-size rejections return `400`; execution deadline returns `422`; overload returns `503`. See the artifact for all response schemas.

## Root snapshot

`file_path` is required and identifies only the configured `BEANCOUNT_FILE` basename (use its actual name if not `main.bean`). The response remains `{"content":"..."}`. It is a source-preserving standalone ledger: includes/globs expand on the server, permitted Python plugin results are materialized, and the client receives no active include or Python plugin directives. It is not a raw file download API. Included files, the manual file when it is not the configured root, unrelated paths and all HTTP glob patterns are rejected. Unsafe paths fail before reading, and the descriptor-based capture rejects symlinks and unsafe includes.

A build failure returns `422` with `detail.message` and `detail.errors`, never fallback raw source. The snapshot retains the original semantics of unchanged directives and handles transformed/generated plugin output under the existing export policy. The ledger workspace remains read-only except for the fixed manual writeback file.

## Writeback guarantees beyond JSON Schema

`POST /xact` receives `{"transactions":["...Beancount text..."]}`. Only completed `*` Cashier transactions with valid stable `cashier_id` and already-known accounts are accepted. It is not an account-creation or general ledger-mutation API. Other directives, incomplete transactions, unknown accounts and malformed/unbalanced transactions are rejected with concrete per-item reasons.

- **Idempotency:** same `cashier_id` plus equivalent normalized transaction content returns the ID in `synchronized` without appending. Whitespace/metadata ordering is not the identity of a transaction. Changed content under an existing ID is a conflict in `rejected`; the server does not overwrite. IDs are case-sensitive. Clients must retain the ID for retries.
- **Partial success:** a mixed batch can return HTTP 200 with both `synchronized` and `rejected`. A successful HTTP status is not confirmation of every item. An unrecoverable ID is represented by `cashier_id: null`. Full-pipeline failure can reject all pending new items; existing equivalent IDs may already be synchronized. Rejections are not a separate HTTP 409 channel.
- **Exact total prices:** `@@ 1.00 USD` remains that total in the manual file and root snapshot, never a rounded unit price such as `@ 0.3333 USD`. Explicit `@` stays unit-price notation. Different total, currency or notation is not an equivalent retry even if parsed rounded unit prices compare equal. Existing lossy historical entries are not automatically repaired.
- **Single-file safety:** the target comes only from `BEANCOUNT_MANUAL_TRANSACTIONS_FILE`; clients cannot supply it. Under the cross-process file lock, the server rereads, deduplicates, validates candidates as part of the configured full root, and durably updates only the mounted manual file. No writable ledger directory or workspace temp file is required. The protocol is crash-conscious, not crash-atomic. Recovery markers are preserved; corrupted/incomplete batches fail closed.
- **Visibility:** after a successful write the server attempts to refresh its BQL cache. A refresh failure does not retract or falsely deny a durable commit. The client pulls the full root snapshot and confirms synchronized IDs. Snapshot reads use the authoritative files, not the BQL cache.

## Limits and error handling

The artifact's `x-cashier-limits` records limits that cannot all be represented as JSON Schema. In particular UTF-8 byte limits must not be confused with string character counts.

- HTTP: 1 MiB request body, 64 KiB query string, 15-second body receive deadline. Size/deadline rejection can happen before authentication or JSON parsing (`413`/`408`). Invalid Host/Content-Length returns `400`.
- BQL: 16 KiB UTF-8, 512 AST nodes/depth 32, 5-second wall and CPU limits, 1 GiB address space, 64 MiB serialized dataset, at most 10,000 rows and 8 MiB HTTP JSON. Timed-out workers are killed/reaped; changing the query grammar does not remove isolation.
- Admission: four shared heavy-operation slots per server process, 64 concurrent connections/tasks at the built-in Uvicorn entry point. Uvicorn may generate a plain-text `503` before any app route, including health.
- Writeback: 100 items per request; each at most 64 KiB UTF-8, 256 postings and 64 metadata fields. Metadata key length is at most 64 characters; string values at most 1024 UTF-8 bytes. `cashier_id` is 1–128 ASCII characters, starts with a letter/digit and then uses letters/digits or `._:-`. The batch shape is schema-validated (`422`); item rules are per-item rejections at HTTP 200.
- Ledger capture: 16 MiB/file, 64 MiB captured/expanded input, 4096 files/include visits and 64 include levels. Capture/pipeline errors fail closed.

Configuration/filesystem/pipeline errors can return `500` with string detail. FastAPI request validation returns `422` with a detail array; snapshot build failures use a structured detail object, while BQL timeout uses string detail. Clients must inspect the documented status-specific shape rather than assume one generic error object.

## Updating the artifact

After an intentional contract change, regenerate deterministically from the repository root:

```sh
uv run --locked python -c 'import json; from pathlib import Path; import main; Path("docs/api/openapi-v1.json").write_text(json.dumps(main.app.openapi(), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")'
uv run --locked pytest tests/test_api_contract.py tests/test_beancount.py tests/test_query_security.py
```

Review the generated diff together with behavior changes. Updating the artifact alone is not evidence of compatibility: value regressions, idempotency, partial-failure, exact-price and forbidden-source tests remain necessary.
