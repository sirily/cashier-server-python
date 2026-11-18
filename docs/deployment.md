# Deployment

Run Cashier Server on Linux. The supplied container runs as a non-root user. Snapshot capture uses descriptor-relative no-follow reads and `/proc/self/fd`; writeback also uses `flock`. On Windows, use a Linux container (for example, with WSL2) rather than running the server natively.

## Docker Compose

Use a published final image from `ghcr.io/sirily/cashier-server-python`, pinned to an explicit release tag or digest. This example assumes the reverse proxy is on the same private Docker network. It does not publish the server port to the host.

```yaml
services:
  cashier-server:
    image: ghcr.io/sirily/cashier-server-python:${CASHIER_SERVER_VERSION:?set a published release version}
    environment:
      BEANCOUNT_FILE: /workspace/main.bean
      CASHIER_API_TOKEN: ${CASHIER_API_TOKEN:?set a deployment token}
    volumes:
      - type: bind
        source: ${CASHIER_LEDGER_DIR:?set an absolute host directory}
        target: /workspace
        read_only: true
        bind:
          create_host_path: false
```

Set `CASHIER_SERVER_VERSION` to the published image tag (without `v`), `CASHIER_LEDGER_DIR` to the absolute path of your Beancount directory, and `CASHIER_API_TOKEN` through your deployment's secret configuration. The root and all included files must be readable by the container user. Use real directories and files, not symlinks, for the configured paths and included sources. Keep secrets, backups, and unrelated files outside the ledger tree. Plugins run as trusted installed server code, not in a sandbox; see the [snapshot policy](architecture.md#ledger-snapshot).

Serve the PWA and API on the same HTTPS origin. Terminate TLS at the reverse proxy and forward over private HTTP, for example to `cashier-server:3000`. If the public URL is `/api/xact`, strip `/api` before forwarding so the server receives `/xact`. Set appropriate proxy body, connection, and timeout limits; they are independent of the [server limits](api.md#limits-and-errors). The app does not provide direct TLS or CORS.

`GET /health` is an unauthenticated process probe. To verify the ledger, make an authenticated `GET /infrastructure?file_path=main.bean` request using your root file's actual basename. Startup and health do not validate the book.

## Enable manual transaction writeback

For read-only sync, leave `BEANCOUNT_MANUAL_TRANSACTIONS_FILE` unset and keep the single read-only mount above. To enable writeback:

1. Create `manual_transactions.bean` in the host ledger directory if it does not already exist. Give the container user read/write access to that file, without making the ledger directory writable in the container.
2. Include it **exactly once** in the root ledger. If an existing glob already includes the file, adjust the glob before adding an explicit include:

   ```beancount
   include "manual_transactions.bean"
   ```

3. Add `BEANCOUNT_MANUAL_TRANSACTIONS_FILE: /workspace/manual_transactions.bean` to the service environment and the following second mount to `volumes`. Keep the parent mount read-only:

   ```yaml
   - type: bind
     source: ${CASHIER_LEDGER_DIR:?set an absolute host directory}/manual_transactions.bean
     target: /workspace/manual_transactions.bean
     read_only: false
     bind:
       create_host_path: false
   ```

The server only appends to that file; it never needs a writable directory under `/workspace`. The file must not be the ledger root. All writers must honor the same exclusive file lock. Before enabling writes, correct invalid or duplicate `cashier_id` values in existing manual transactions; the server refuses to write rather than rewriting IDs. See the [writeback API](api.md#writeback-guarantees-beyond-json-schema) for accepted IDs and transaction formats.

## Recover interrupted writeback

Do not edit or remove the reserved `; cashier-writeback-v1` checksum comments. An incomplete or altered batch is detected on the next write, and further writes are refused. The append protocol is crash-conscious, not crash-atomic.

Stop every writer, preserve a copy of the damaged file outside the ledger tree, restore a verified known-good manual file, and validate the full ledger before restarting. Do not truncate an unverified suffix or remove a marker just to clear an error. If you restored the file by replacing it on the host, recreate the container so its file bind mount picks up the replacement.

## Upgrade an older deployment

Move TLS termination to the external proxy and serve the PWA and API on the same origin. Remove `CASHIER_SSL_KEY`, `CASHIER_SSL_CERT`, certificate mounts used only by this server, and `CASHIER_CORS_ORIGINS`. The `/hello` and `/shutdown` routes are no longer available. Remove `CASHIER_ENABLE_SHUTDOWN` and stop the server with the container runtime or service manager. Ledger CLI is not needed. See [API migration](api.md#intentional-v1--v2-migration) for changes to client requests and health checks.
