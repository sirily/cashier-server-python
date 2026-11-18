# Cashier Server Python

A FastAPI server that syncs a Beancount ledger with the [Cashier PWA](https://github.com/sirily/cashier-sveltekit). It exports one standalone ledger for offline use and can append completed Cashier transactions to a dedicated file.

## Run locally

From this checkout on Linux, with [uv](https://docs.astral.sh/uv/) and Python 3.12 or newer, set these variables in your environment or in a local `.env` file:

```dotenv
BEANCOUNT_FILE=/path/to/ledger/main.bean
CASHIER_API_TOKEN=replace-with-a-long-random-token
```

```sh
uv sync --locked
uv run --locked cashier-server
```

The server listens on `0.0.0.0:3000` over HTTP. Send `Authorization: Bearer <token>` on API requests; `GET /health` is public and checks the process, not the ledger. Keep port 3000 private. For Docker, HTTPS, or enabling transaction writeback, see [Deployment](docs/deployment.md).

## API

| Operation | Server path |
| --- | --- |
| Download the standalone ledger | `GET /infrastructure?file_path=main.bean` |
| Sync manual transactions | `POST /xact` |
| Check the running process | `GET /health` |
| Check bearer authentication | `GET /ping` |

Use the configured root file's basename in `file_path`. Paths are relative to the server; a reverse proxy can expose them under `/api`. See the [HTTP API guide](docs/api.md) and [OpenAPI contract](docs/api/openapi-v2.json) for payloads, errors, and compatibility.

## Development

Run tests with `uv run --locked pytest`. See [Architecture](docs/architecture.md) for snapshot and writeback internals, [Code intelligence](docs/code-intelligence.md) for editor tools, and [Release policy](docs/releasing.md) for branching and publishing.
