# Synchronization architecture

The server owns the authoritative Beancount sources. Cashier pulls a standalone snapshot, stores it in OPFS, and parses it locally with RustLedger. To sync local transactions, the PWA submits them to `POST /xact`, then pulls the full snapshot again to confirm their `cashier_id` values. There is no persistent server-side ledger cache or reload step.

For HTTP behavior, see the [API guide](api.md). For mounts, proxy configuration, and recovery, see [Deployment](deployment.md).

## Ledger snapshot

`GET /infrastructure` builds a fresh snapshot for the configured ledger root. `file_path` selects only the root basename, not an arbitrary file or glob.

1. [`workspace.py`](../cashier_snapshot/workspace.py) captures the root and its relative include/glob graph with bounded no-follow reads. It stages private copies outside the ledger tree. Capture does not provide a filesystem-wide atomic snapshot against unrelated writers.
2. [`source_index.py`](../cashier_snapshot/source_index.py) indexes the original directive text. [`booking_baseline.py`](../cashier_snapshot/booking_baseline.py) runs core booking without plugins. [`builder.py`](../cashier_snapshot/builder.py) then loads the staged book with the full Beancount pipeline. Only the configured plugins allowed by [`plugin_policies.py`](../cashier_snapshot/plugin_policies.py) may participate in export; inclusion in the allowlist does not make all transformations safe.
3. [`reconcile.py`](../cashier_snapshot/reconcile.py) compares source, booked, and materialized entries by source origin. It retains unchanged source blocks verbatim, preserving price syntax such as `@@` and booking syntax such as `{}` when plugin materialization leaves the booked result unchanged. Generated entries and safe transformations go through the [snapshot printer](../cashier_snapshot/printer.py). If matching or transformation would lose accounting meaning, such as reprinting a transformed source entry with exact `@@` syntax, the build fails.
4. The output contains no active `include` or Python `plugin` directives. It contains the materialized result of `pad`, not the consumed `pad` directive. Unsupported auxiliary sources (such as `Document` directives or `option "documents"`) and workspace Python imports via `insert_pythonpath` are rejected. On failure, the API returns an error, not a raw root or stale snapshot.

Round-tripping every directive through the Python printer would change accounting semantics: Python Beancount normalizes total prices to unit prices during parsing, so printing the normalized value can lose the exact original total. Keep the source-preserving path and the container acceptance check that parses the result with `@rustledger/wasm@0.14.1` with zero parse errors.

## Manual transaction writeback

[`writeback.py`](../writeback.py) implements one append-only write channel. Each item must be one completed `*` transaction with a stable `cashier_id` and accounts already present in the ledger. Incomplete `!` transactions and non-transaction directives are rejected. There is no account creation, arbitrary editing, deletion, or client-selected destination.

- **Lock and reread.** Pin and exclusively lock the configured manual file, verify the integrity of existing batches, and reread authoritative sources under the lock. The manual file must be inside the configured ledger root's directory and included exactly once in the full ledger.
- **Classify and validate.** Equivalent content for an existing ID is already synchronized; different content is a conflict, never an overwrite. Exact total prices (`@@`) and explicit unit prices (`@`) retain their notation and participate in equivalence checks. Reject invalid items with reasons and keep valid items eligible for commit. Validate the candidate through the uncached full Beancount pipeline using a private copy outside the ledger tree. Full-ledger validation can reject all pending additions.
- **Commit and confirm.** Durably append accepted entries to the one mounted file under the lock, then acknowledge new IDs. No temporary file or atomic rename in the ledger workspace is needed. Checksummed batch markers detect interrupted writes; the protocol is not crash-atomic. Clients must check both `synchronized` and `rejected`, then pull the root snapshot to confirm.

Changes to these paths need behavioral coverage for include/glob handling, plugin materialization, price precision, idempotency and conflicts, mixed batches, and the single-file write boundary. See the [container acceptance instructions](releasing.md#required-checks-and-audit-reporting) for the RustLedger boundary check.
