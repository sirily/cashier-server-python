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

### Configure Certificates

Create a self-signed certificate with OpenSSL.

Add `CASHIER_SSL_KEY` and `CASHIER_SSL_CERT` paths to `.env` file, pointing to generated `key.pem` and `cert.pem`.

#### Android

On Android, import the `cert.pem`. Usually Settings -> Security -> Install certificate -> CA Certificate.

Then, open the Cashier Server URL in the browser and accept the self-signed certificate.

## Run

Execute the `cashier-server` script provided by the `cashier-server` package.

The server runs on 0.0.0.0:3000, matching the Rust implementation.

## API Endpoints

- `/?query=SELECT...` - Execute a Beancount BQL query, including Accounts and Payees queries
- `/ping` - Simple authenticated liveness check
- `/health` - Unauthenticated container health/status check
- `/reload` - Reload the Beancount data
- `/infrastructure?file_path=config.bean` - Return a Beancount workspace file as `{ "content": "..." }`
- `/infrastructure?file_path=prices/*.bean` - Return matching Beancount workspace files as `{ "files": [{ "path": "prices/2024.bean", "content": "..." }] }`
- `/xact` - Stage 2 manual transaction writeback endpoint. Deployment exposes it as `/api/xact`; it accepts only Cashier-created completed transaction directives with `cashier_id` and writes only to the configured `BEANCOUNT_MANUAL_TRANSACTIONS_FILE`.

`/hello` and `/shutdown` have been removed and return `404` for authenticated requests. Stop the process through your service manager or container runtime; `CASHIER_ENABLE_SHUTDOWN` is no longer a supported setting.

CORS allows all origins by default. Set `CASHIER_CORS_ORIGINS` to a comma-separated origin list to restrict it; wildcard origins do not enable credentials.

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

Do not configure a writable directory or client-controlled write path.

Writeback preserves exact total prices (`@@ 1.00 USD`) in the manual file and the standalone snapshot, without deriving them from rounded unit prices. Explicit unit prices (`@`) remain unit prices. Retries retain the same `cashier_id` without appending another copy; changing the total, currency, or price notation under an existing ID is a conflict. Previously stored `@` entries are not automatically converted back to `@@`: the original total cannot be recovered reliably from a rounded unit price and must be reviewed by the owner.

## Security boundaries and limits

- Run the server on Linux (the supplied container is supported). Safe reads require `O_NOFOLLOW`, descriptor-relative opens and `/proc/self/fd`; writeback additionally requires POSIX `flock`. Native Windows execution is not supported by the hardened filesystem protocol.
- Use HTTPS at the reverse proxy or configure both TLS files. Do not expose plain HTTP port 3000 publicly. Keep proxy/body/connection limits enabled and restrict CORS to the PWA origin where possible.
- The health exemption uses the ASGI path, never a URL reconstructed from `Host`. Malformed or duplicate Host headers are rejected; protected routes always require the bearer token.
- BQL is a read-only channel: `SELECT`, `BALANCES`, `JOURNAL`, and `PRINT` over the initial Beancount tables. `CREATE`, `INSERT`, external data sources and mutable shared query tables are forbidden. Writing from the PWA still uses `/xact`, not BQL.
- BQL workers have a 5-second execution deadline, 1 GiB address-space limit, 16 KiB query/512-node AST limits, and 10,000-row/8 MiB HTTP JSON response limits (the internal worker response is bounded separately). A timeout kills and reaps the worker. Expensive operations have four admission slots per server process; excess work receives `503` rather than waiting unboundedly. The built-in entry point also limits concurrent connections to 64.
- Requests are capped at 1 MiB and query strings at 64 KiB; receiving the body has a 15-second deadline. `/xact` accepts at most 100 items, 64 KiB per transaction, 256 postings and 64 metadata fields. `cashier_id` is a case-sensitive ASCII key, at most 128 characters, beginning with a letter or digit and containing only letters, digits, `.`, `_`, `:`, or `-`. Existing noncanonical IDs must be corrected by the owner before writeback is enabled; IDs are never silently rewritten.
- Legacy infrastructure reads return only nonhidden `.bean`/`.beancount` files. Secrets, backups and unrelated files belong outside the ledger directory. Glob responses remain raw source; a literal root request produces the standalone snapshot. Neither mechanism follows symlinks. Keep the configured root and every parent path free of symlinks.
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
