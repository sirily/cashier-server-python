# cashier-server-python

Cashier synchronization server, in Python

Ledger-cli REST server for [Cashier](https://github.com/alensiljak/cashier) PWA, implemented in Python with FastAPI.

Cashier Server acts as a mediator between Cashier PWA and Ledger CLI, forwarding queries to Ledger and the results to Cashier. Used for synchronizing the ledger data in Cashier.

This is a Python implementation of the Cashier Server using FastAPI.

## Installation

1. Install `uv`
2. Install `uv tool install cashier-server`

### Configure Backend

To use Beancount as a back-end, set the `BEANCOUNT_FILE` environment variable.

The easiest way is by creating an `.env` file containing this variable, 
which should point to your Beancount book.
Otherwise, Cashier Server will use Ledger as the backend.

### Configure Certificates

Create a self-signed certificate with OpenSSL.

Add `CASHIER_SSL_KEY` and `CASHIER_SSL_CERT` paths to `.env` file, pointing to generated `key.pem` and `cert.pem`.

#### Android

On Android, import the `cert.pem`. Usually Settings -> Security -> Install certificate -> CA Certificate.

Then, open the Cashier Server URL in the browser and accept the self-signed certificate.

## Run

Execute `cashier-server-py` script provided by the `cashier-server` package.

The server runs on 0.0.0.0:3000, matching the Rust implementation.

## API Endpoints

- `/` - Execute a ledger command
- `/hello` - Return a base64-encoded image
- `/ping` - Simple health check
- `/reload` - Reload the Beancout data
- `/shutdown` - Request server shutdown
- `/infrastructure?file_path=config.bean` - Return a Beancount workspace file as `{ "content": "..." }`
- `/infrastructure?file_path=prices/*.bean` - Return matching Beancount workspace files as `{ "files": [{ "path": "prices/2024.bean", "content": "..." }] }`

CORS is enabled for all origins, similar to the Rust implementation.

## Architecture documents

- [`docs/architecture/standalone-pwa-ledger-snapshot.md`](docs/architecture/standalone-pwa-ledger-snapshot.md) — design for producing one source-preserving standalone `main.bean` for offline Cashier PWA sync.
- [`docs/architecture/standalone-pwa-ledger-implementation-plan.md`](docs/architecture/standalone-pwa-ledger-implementation-plan.md) — staged implementation and verification plan.
- [`docs/architecture/production-plugin-export-policy-audit.md`](docs/architecture/production-plugin-export-policy-audit.md) — required plugin behavior audit before changing the production export path.

## Development

VSCode recommended.
Run the `run.cmd` script to start the server.
Or run from VSCode to debug.

### Debug

Make sure that Beancount is configured and can be called from the current directory.
Then run:

```sh
run.cmd
# or
uv run python app.py
# Or uvicorn directly:
uvicorn app:app --host 0.0.0.0 --port 3000
```

## Notes on the Implementation

- Logging is configured to output to the console.
- For the `/hello` endpoint, you would need to provide an actual image file named "hello.png" in the same directory as the app.py file.
