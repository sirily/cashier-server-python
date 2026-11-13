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

VSCode recommended.
Run the `run.cmd` script to start the server.
Or run from VSCode to debug.

### Debug

Configure `BEANCOUNT_FILE` and `CASHIER_API_TOKEN`, then run from the repository root:

```sh
uv run cashier-server
# Or run the same entry point directly:
uv run python main.py
```

On Windows, `run.cmd` runs the same entry point. Use this entry point rather than a bare `uvicorn main:app` invocation so that the BQL connection is preloaded and configuration is checked before serving requests.

## Notes on the Implementation

- Logging is configured to output to the console.
