# Cashier Server Python

Cashier Server Python is the API/server component for Cashier.

## Running Python

- Use `uv` to run Python code.

## Running Tests

- The tests are in `/tests` directory.
- Use `uv run pytest` to run tests.

## Tools

- Use ripgrep (`rg`) for fast text search across files.

## Architecture docs

- Current architecture docs are in `/docs/architecture/`.
- Stage 1 source-preserving standalone PWA ledger snapshot is documented in `/docs/architecture/standalone-pwa-ledger-snapshot.md` and `/docs/architecture/standalone-pwa-ledger-implementation-plan.md`.
- Stage 2 manual transaction writeback plan for code agents is `/docs/architecture/stage-2-manual-transaction-writeback.md`.

## Stage 1 accepted boundary

Stage 1 is accepted in production:

- `BEANCOUNT_FILE=/workspace/main.bean` points at the real Beancount root.
- The server builds a source-preserving standalone snapshot for the PWA.
- The PWA stores the snapshot in OPFS and parses it with `@rustledger/wasm@0.14.1` with `0` parse errors.
- The ledger workspace is read-only for Stage 1.

Do not regress source-preserving behavior, Python plugin materialization policy, include/glob handling, or RustLedger boundary tests when adding writeback.

## Stage 2 manual transaction writeback

Before implementing Stage 2 code, read `/docs/architecture/stage-2-manual-transaction-writeback.md`.

Stage 2 adds exactly one narrow write channel:

```http
POST /api/xact
```

Depending on FastAPI routing, implement the app route as `/xact`; deployment exposes it under `/api/xact`.

The endpoint accepts only Cashier-created completed `*` transaction directives with stable `cashier_id` metadata. It writes only to the fixed configured file:

```text
BEANCOUNT_MANUAL_TRANSACTIONS_FILE=/workspace/manual_transactions.bean
```

Deployment contract:

```yaml
volumes:
  - /mnt/raid4t/homelab/appdata/lazybean:/workspace:ro
  - /mnt/raid4t/homelab/appdata/lazybean/manual_transactions.bean:/workspace/manual_transactions.bean:rw
```

`main.bean` includes the file once:

```beancount
include "manual_transactions.bean"
```

Rules that code agents must follow:

- Do not introduce a writable directory.
- Do not introduce a `cashier-writeback` directory.
- Do not write temp files into `/workspace`.
- Do not accept a target path from client input.
- Do not implement a general Beancount mutation API.
- Reject `include`, `plugin`, `option`, `open`, `close`, `balance`, `pad`, and any non-transaction directive in `/api/xact` payloads.
- v1 accepts only completed `*` transactions, not incomplete `!` transactions.
- Accounts must already exist in the server ledger; account creation from mobile is out of scope.
- Use `cashier_id` for idempotency and deduplication.
- Same `cashier_id` + same transaction means already synchronized; return it as synchronized without appending.
- Same `cashier_id` + different transaction means conflict; reject and never overwrite automatically.
- Mixed batch behavior is partial success: valid transactions commit, invalid transactions are rejected with concrete reasons.
- Before committing, validate the candidate `manual_transactions.bean` as part of the configured full `main.bean` pipeline.

Single-file write protocol:

```text
lock
  reread current manual_transactions.bean
  deduplicate by cashier_id
  build candidate content in memory or non-ledger private temp storage
  validate candidate as the configured full ledger
  update mounted manual_transactions.bean under the lock using a crash-conscious single-file write
unlock
```
