# Cashier Stage 2: server manual transaction writeback

Created: 2026-05-28
Status: implementation-ready plan for code agents

## Purpose

Stage 1 is accepted in production: the Python server provides a source-preserving standalone Beancount snapshot that the Cashier PWA can pull into OPFS and parse with `@rustledger/wasm@0.14.1` with `0` parse errors.

Stage 2 adds one narrow write channel for manual transactions created by the PWA. The server must append validated Cashier-created transactions to exactly one configured file, `manual_transactions.bean`, then continue serving the full source-preserving ledger through the existing Stage 1 infrastructure path.

This is not a general Beancount mutation API.

## API endpoint

Use the short endpoint name:

```http
POST /api/xact
```

Depending on the FastAPI router prefix, the app-level route should be `/xact`; deployment exposes it under `/api/xact` through Caddy/Pangolin routing.

## Request scope

Stage 2 v1 accepts only Cashier-created manual transaction directives:

- only Beancount transaction directives are accepted;
- only completed `*` transactions are accepted;
- incomplete `!` transactions are out of scope;
- `include`, `plugin`, `option`, `open`, `close`, `balance`, `pad`, and all other control directives are rejected;
- mobile creation of new accounts is out of scope, so referenced accounts must already exist in the authoritative server ledger;
- every transaction must have a valid metadata entry:

```beancount
cashier_id: "UUID"
```

`cashier_id` is the idempotency key. Optional diagnostic metadata such as `cashier_device_id` or `cashier_created_at` may exist, but must not replace `cashier_id`.

## Response model

Return synchronized IDs and rejected items with concrete human-readable reasons:

```json
{
  "synchronized": ["transaction-uuid-1"],
  "rejected": [
    {
      "cashier_id": "transaction-uuid-2",
      "reason": "Транзакция не сбалансирована: сумма по RUB равна 50.00 RUB"
    }
  ]
}
```

The server does not need to tell the PWA whether an ID was newly appended or was already present. Both are `synchronized` because the PWA's next step is to pull the full ledger and confirm that `cashier_id` appears in parsed `main.bean`.

## Deduplication and conflicts

For each incoming transaction:

1. If `cashier_id` does not exist in `manual_transactions.bean`, validate and append it if valid.
2. If `cashier_id` already exists with the same transaction, return it as `synchronized` without appending another copy.
3. If `cashier_id` already exists with different transaction content, reject it as a conflict. Never overwrite automatically.

"Same transaction" should be implemented in a user-safe way. Prefer comparing parsed normalized transaction fields over literal text formatting, because metadata order, whitespace, and printer formatting may change. If a first implementation cannot safely normalize, fail closed with a conflict rather than overwriting or duplicating.

## Batch behavior

Mixed batches are allowed and are already part of the accepted design:

- valid transactions commit;
- invalid transactions stay out of the file and are returned in `rejected` with reasons;
- one bad local entry must not block all correct purchases.

The final write should commit all accepted valid new transactions from the batch together under the file lock.

## Filesystem and deployment contract

The deployment intentionally exposes exactly one writable Beancount file to the server. Do not introduce a writable directory for writeback.

```text
/workspace                         read-only real ledger
/workspace/manual_transactions.bean the only rw bind-mounted writeback file
```

Expected compose shape:

```yaml
volumes:
  - /mnt/raid4t/homelab/appdata/lazybean:/workspace:ro
  - /mnt/raid4t/homelab/appdata/lazybean/manual_transactions.bean:/workspace/manual_transactions.bean:rw
```

The authoritative `main.bean` includes the file once:

```beancount
include "manual_transactions.bean"
```

The server receives a fixed path from configuration, never from the client:

```text
BEANCOUNT_MANUAL_TRANSACTIONS_FILE=/workspace/manual_transactions.bean
```

Rules for code agents:

- Do not add a `cashier-writeback` directory.
- Do not require a writable ledger directory.
- Do not write temp files into `/workspace`.
- Do not accept a manual transaction file path from the request body or query string.

## Single-file write protocol

Because only one file is writable, do not rely on directory-level atomic rename in the ledger workspace. Use this fixed single-file protocol:

```text
lock
  reread current manual_transactions.bean
  parse existing manual transactions and cashier_id metadata
  classify incoming transactions as already synchronized, conflict, rejected, or valid-new
  build candidate manual_transactions.bean content in memory or non-ledger private temp storage
  validate candidate as part of the configured full ledger/main.bean pipeline
  validate downstream PWA boundary: source-preserving snapshot still parses with RustLedger expectations where tests cover it
  update the mounted manual_transactions.bean under the lock using a crash-conscious single-file write
unlock
```

Crash-conscious single-file write means the implementation must deliberately choose safe behavior for the bind-mounted file. It may use a process-private temp file outside `/workspace` for candidate construction, but the ledger workspace must only receive the final content in `manual_transactions.bean`.

## Validation checklist before committing

Before changing the writable file, the backend must verify:

1. Payload contains only transaction directives.
2. Every transaction has a valid `cashier_id`.
3. Duplicate IDs inside one request are deterministic: same transaction can collapse; different transaction is rejected.
4. Existing equivalent ID in `manual_transactions.bean` counts as `synchronized` without appending.
5. Existing ID with different content returns `rejected` conflict.
6. Accounts already exist in the server ledger.
7. Transaction parses and is accounting-valid; simple single-currency postings balance.
8. Candidate `manual_transactions.bean` validates as part of the real `main.bean` through the server Beancount/materialization pipeline.
9. The resulting full ledger snapshot remains acceptable to the PWA RustLedger boundary.

## Acceptance scenarios for server agents

A Stage 2 server implementation is not accepted until these scenarios pass:

1. Normal expense: `POST /api/xact` appends exactly once to `manual_transactions.bean`; subsequent full-ledger pull includes the transaction.
2. Retry with same `cashier_id` and same transaction returns `synchronized` and does not append a duplicate.
3. Retry with same `cashier_id` and different transaction returns a conflict rejection and does not overwrite.
4. Rejected unbalanced transaction does not change `manual_transactions.bean` and returns the concrete validation reason.
5. Payload containing `include`, `plugin`, `option`, `open`, `close`, `balance`, `pad`, or any other non-transaction directive is rejected.
6. Transaction using an unknown account is rejected.
7. Mixed batch commits valid transactions and rejects invalid ones with reasons.
8. The full source-preserving snapshot served after writeback parses through the PWA/RustLedger boundary with `0` parse errors.

## Non-goals

Do not implement any of these in Stage 2 v1:

- generic Beancount file editing;
- client-controlled file paths;
- writable directory deployment;
- `cashier-writeback` directory;
- account creation from mobile;
- editing or deleting arbitrary server transactions;
- incomplete `!` transaction support;
- automatic conflict overwrite.
