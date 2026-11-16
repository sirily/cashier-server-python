# cashier-server-python

Cashier synchronization server, in Python

Beancount synchronization server for [Cashier](https://github.com/alensiljak/cashier) PWA, implemented in Python with FastAPI.

The server executes BQL queries for Accounts and Payees, serves a source-preserving standalone Beancount snapshot for offline sync, and accepts the narrow manual transaction writeback channel. Beancount is the only supported backend; Ledger CLI is not used.

## Installation

1. Install `uv`
2. Install `uv tool install cashier-server`

### Configure Beancount and authentication

Set `BEANCOUNT_FILE` to the root of your Beancount book and `CASHIER_API_TOKEN` to a deployment-scoped bearer token. You can set these environment variables in an `.env` file in the working directory.

Both settings are required: the server refuses to start without either one. If imported as an ASGI application without `BEANCOUNT_FILE`, BQL and reload requests return HTTP 500 with `BEANCOUNT_FILE must be configured`, never a Ledger CLI fallback.

Send `Authorization: Bearer <your-token>` on API requests. `/health` is available without authentication for container probes.

### HTTPS and same-origin deployment

Terminate HTTPS at the external reverse proxy and forward requests through Caddy to the server over internal HTTP. Serve the PWA and API under the same browser origin, exposing the API under `/api/`.

The server does not load TLS certificates or provide CORS support. Direct HTTPS startup and separate-origin browser clients are no longer supported. Remove `CASHIER_SSL_KEY`, `CASHIER_SSL_CERT`, certificate/key mounts used only by this server, and `CASHIER_CORS_ORIGINS` from existing deployments; those environment variables are no longer read.

## Run

Execute the `cashier-server` script provided by the `cashier-server` package.

The server listens for internal HTTP on 0.0.0.0:3000. Do not expose this port publicly.

## API Endpoints

The versioned [OpenAPI v1 contract](docs/api/openapi-v1.json) describes requests, responses, bearer authentication, errors and limits. [Server guarantees and compatibility](docs/api/README.md) cover idempotency, partial success and exact `@@` totals. Contract paths are server-relative; deployment adds `/api` externally.

- `/?query=SELECT...` - Execute a read-only BQL `SELECT` from `accounts` or `transactions` for Sync Accounts and Payees
- `/ping` - Simple authenticated liveness check
- `/health` - Unauthenticated container health/status check
- `/reload` - Reload the Beancount data
- `/infrastructure?file_path=main.bean` - Return the standalone snapshot of the configured `BEANCOUNT_FILE` as `{ "content": "..." }`. Use the configured root's filename if it is not `main.bean`. Individual files and glob requests are not supported.
- `/xact` - Stage 2 manual transaction writeback endpoint. Deployment exposes it as `/api/xact`; it accepts only Cashier-created completed transaction directives with `cashier_id` and writes only to the configured `BEANCOUNT_MANUAL_TRANSACTIONS_FILE`.

`/hello` and `/shutdown` have been removed and return `404` for authenticated requests. Stop the process through your service manager or container runtime; `CASHIER_ENABLE_SHUTDOWN` is no longer a supported setting.

## Stage 2 manual transaction writeback

Stage 2 is documented in `docs/architecture/stage-2-manual-transaction-writeback.md`.

The intended deployment keeps the ledger read-only except for exactly one file:

```yaml
volumes:
  - /mnt/raid4t/homelab/appdata/lazybean:/workspace:ro
  - /mnt/raid4t/homelab/appdata/lazybean/manual_transactions.bean:/workspace/manual_transactions.bean:rw
```

`main.bean` includes:

```beancount
include "manual_transactions.bean"
```

The deployment environment must include:

```yaml
environment:
  BEANCOUNT_FILE: /workspace/main.bean
  BEANCOUNT_MANUAL_TRANSACTIONS_FILE: /workspace/manual_transactions.bean
  CASHIER_API_TOKEN: <supply-through-your-deployment-secret-configuration>
```

The root book and manual file must exist before starting the container. The image runs as a non-root user, which needs read access to the book and write access to the single manual file. Do not configure `CASHIER_ENABLE_SHUTDOWN` or install Ledger CLI.

The Dockerfile installs locked production dependencies in a separate builder stage with Git and uv. The final stage copies only the non-editable application environment at `/app/.venv`; Git, uv, pip, build caches, and development tools are not included. Runtime starts `cashier-server` directly, without installing or resolving dependencies. Curl remains available for the container health check.

Do not configure a writable directory or client-controlled write path.

Writeback preserves exact total prices (`@@ 1.00 USD`) in the manual file and the standalone snapshot, without deriving them from rounded unit prices. Explicit unit prices (`@`) remain unit prices. Retries retain the same `cashier_id` without appending another copy; changing the total, currency, or price notation under an existing ID is a conflict. Previously stored `@` entries are not automatically converted back to `@@`: the original total cannot be recovered reliably from a rounded unit price and must be reviewed by the owner.

## Security boundaries and limits

- Run the server on Linux (the supplied container is supported). Safe reads require `O_NOFOLLOW`, descriptor-relative opens and `/proc/self/fd`; writeback additionally requires POSIX `flock`. Native Windows execution is not supported by the hardened filesystem protocol.
- Use HTTPS at the external reverse proxy and keep HTTP port 3000 on the private proxy/container network. Serve the PWA and API same-origin; the server sends no CORS permission headers. Keep proxy/body/connection limits enabled.
- The health exemption uses the ASGI path, never a URL reconstructed from `Host`. Malformed or duplicate Host headers are rejected; protected routes always require the bearer token.
- BQL is a narrow read-only Sync channel, not a general query interface. `SELECT` requires an explicit `FROM accounts` or `FROM transactions` (`#accounts` and `#transactions` are also accepted), including nested SELECTs. The one compatibility exception is `SELECT sum(number), currency, account ORDER BY account`: it aggregates posting amounts by currency/account and returns `[sum, currency, account]` rows sorted by account. It uses `postings`, not `accounts`; no other implicit or explicit postings queries are enabled. `BALANCES`, `JOURNAL`, `PRINT`, other tables, implicit postings, expression-based `FROM`, and `OPEN`/`CLOSE`/`CLEAR` qualifiers are rejected with HTTP 400. Existing Accounts and Payees queries remain supported; clients using the removed forms must switch to the supported Sync queries. `CREATE`, `INSERT`, external data sources and mutable shared query tables remain forbidden. Writing from the PWA still uses `/xact`, not BQL.
- BQL workers have a 5-second execution deadline, 1 GiB address-space limit, 16 KiB query/512-node AST limits, and 10,000-row/8 MiB HTTP JSON response limits (the internal worker response is bounded separately). A timeout kills and reaps the worker. Expensive operations have four admission slots per server process; excess work receives `503` rather than waiting unboundedly. The built-in entry point also limits concurrent connections to 64.
- Requests are capped at 1 MiB and query strings at 64 KiB; receiving the body has a 15-second deadline. `/xact` accepts at most 100 items, 64 KiB per transaction, 256 postings and 64 metadata fields. `cashier_id` is a case-sensitive ASCII key, at most 128 characters, beginning with a letter or digit and containing only letters, digits, `.`, `_`, `:`, or `-`. Existing noncanonical IDs must be corrected by the owner before writeback is enabled; IDs are never silently rewritten.
- `/infrastructure` serves only the configured root's standalone snapshot; `file_path` remains required and must identify that root by its filename, relative to its directory. Other paths, including included files, `manual_transactions.bean` when it is not the configured root, and all glob patterns, return HTTP 403. Multi-file HTTP import is no longer supported: clients must request the root snapshot. The server still expands the root's includes/globs and executes configured plugins while building the snapshot, without exposing raw source files. Build errors return HTTP 422, never raw-source fallback. Secrets, backups and unrelated files belong outside the ledger directory. Keep the configured root and every parent path free of symlinks.
- The ledger is captured through bounded no-follow reads before loading. Limits are 16 MiB per file (including the manual file and recovery comments), 64 MiB per capture/expanded include graph, 4,096 files/include visits and 64 include levels. Absolute, escaping, encrypted and unsafe includes fail closed. Snapshot, BQL preload and writeback use private captured source copies outside the ledger tree.
- A source-only capture supports relative include/glob files and configured server-installed plugins. `Document` directives, `option "documents"`, `insert_pythonpath`, encrypted roots and environment-expanded root names are not supported; they fail closed rather than silently losing auxiliaries or importing workspace Python. Trusted installed plugins are not sandboxed and can still access resources permitted to the server process. Do not enable untrusted plugins.
- The existing manual file must be included exactly once in the configured full ledger. Each writer holds an exclusive lock on its pinned file descriptor through reread, validation and durable append. All other writers must honor the same lock; the read-only parent mount remains required. Only the mounted manual file is written.

### Interrupted writeback recovery

New batches carry reserved `; cashier-writeback-v1` checksum comments. Catchable append failures roll back only a verified prefix of that attempt to its original length, under the file lock. An interrupted or altered batch is detected on the next write; the server refuses further writes instead of acknowledging incomplete transactions. This is crash-conscious append, not a claim of crash atomicity.

If recovery is required, stop every writer, preserve the damaged file for review outside the workspace, restore a verified known-good manual file, and validate the full ledger before restarting. Never remove an unverified suffix or checksum comments just to clear an error. Keep recovery comments intact in the manual source file. Snapshot/pull preserves transaction semantics and `cashier_id` metadata; presentation of surrounding comments is not a recovery mechanism.

## Architecture documents

- [`docs/architecture/standalone-pwa-ledger-snapshot.md`](docs/architecture/standalone-pwa-ledger-snapshot.md) — design for producing one source-preserving standalone `main.bean` for offline Cashier PWA sync.
- [`docs/architecture/standalone-pwa-ledger-implementation-plan.md`](docs/architecture/standalone-pwa-ledger-implementation-plan.md) — staged implementation and verification plan.
- [`docs/architecture/production-plugin-export-policy-audit.md`](docs/architecture/production-plugin-export-policy-audit.md) — required plugin behavior audit before changing the production export path.
- [`docs/architecture/stage-2-manual-transaction-writeback.md`](docs/architecture/stage-2-manual-transaction-writeback.md) — implementation-ready Stage 2 contract for `/api/xact` writeback into the single writable `manual_transactions.bean` file.

## Development

Read the [release policy](docs/releasing.md) before creating release/work branches,
changing versions, publishing, or deploying. It follows the Cashier PWA release
model: release-branch pushes produce RC images, and squash-merging the release PR
produces the final versioned image and GitHub Release.

[Code-intelligence setup](docs/code-intelligence.md) covers Serena (Python LSP),
GitNexus, the project MCP configuration, and local index maintenance. These tools
are development-only and are not installed in the production image.

Use Zed or VSCode with the locked Python environment. Run the Linux entry point below, or use the supplied container.

### Debug

Configure `BEANCOUNT_FILE` and `CASHIER_API_TOKEN`, then run from the repository root:

```sh
uv run cashier-server
# Or run the same entry point directly:
uv run python main.py
```

Use this entry point rather than a bare `uvicorn main:app` invocation so that the BQL connection is preloaded and configuration is checked before serving requests. On Windows, run the Linux container (for example under WSL2).

## Notes on the Implementation

- Logging is configured to output to the console.
