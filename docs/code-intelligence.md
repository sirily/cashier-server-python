# Code intelligence

Serena and GitNexus are optional development tools for agents and editors, not
server dependencies. They are not added to `pyproject.toml`, `uv.lock`, the wheel,
or the production image. Neither tool needs an API key for the configuration
below. Local indexing does not replace tests.

## Prerequisites and pinned versions

- `uv` and `uvx` on `PATH`. Install them using an official Astral distribution:
  <https://docs.astral.sh/uv/getting-started/installation/>. A per-user installation
  in `~/.local/bin` works across projects without changing the system Python.
- Python 3.13, matching `.python-version`; `uv` can download it automatically.
- Node.js `^22.18.0` or `>=24.11.0`, and `npm` (GitNexus requirements).
- Serena: `serena-agent==2.1.0`.
- GitNexus: `gitnexus@1.6.12`.

Prepare the existing locked application environment from the repository root:

```sh
uv sync --locked
```

The first tool invocation downloads its pinned package and may download Python,
LSP dependencies, and native Node libraries. This requires network access and
uses the normal user caches. No global Serena/GitNexus installation is required.
Keep tool versions aligned between `.mcp.json`, `.zed/settings.json`, and these
commands when deliberately upgrading. Do not use an unpinned `@latest` runner.

## Python environment in Zed

`uv sync --locked` creates the local `.venv` with the application's runtime and
test dependencies. `[tool.pyright]` in `pyproject.toml` points both Zed's
Basedpyright and Serena's Pyright at this environment, using portable relative
paths and standard type checking. It does not disable import or type errors.

If Zed was opened before `.venv` existed, its selected Python toolchain can still
be the system interpreter. In the Python toolchain selector choose
`.venv/bin/python` (`.venv/Scripts/python.exe` on Windows), then run
`editor: restart language server` from the command palette. This also aligns
editor-launched Python tasks with `uv run`. Restart Serena separately if it has
cached the old environment. Activating a terminal alone does not change the
editor's already running language server.

A passing `uv run pytest` is not a clean lint/type-check result: Ruff and
Basedpyright can report existing style or typing problems independently. Fix
those deliberately; do not hide them by disabling diagnostics or importing
packages from the system Python.

## MCP clients

`.mcp.json` configures both stdio servers for clients that support this project
configuration. `.zed/settings.json` configures the same servers for Zed; Zed does
not depend on reading `.mcp.json`. Approve the project configuration when asked,
and reload/restart the context servers after adding or changing it. A running
agent session may need to be restarted before the tools appear.

Both launchers use commands from `PATH`, not a developer's absolute home or
container path. They must run with this repository as the working directory:
Serena uses `--project-from-cwd` to find `.serena/project.yml`. If a client does
not preserve the workspace working directory, use its local configuration to
set the working directory or pass this checkout's absolute path to Serena's
`--project`; do not commit that machine-specific path.

Serena's dashboard/browser startup is disabled. Neither MCP server opens an HTTP
listener in this configuration. Their normal operation is local; do not enable
remote embeddings, wiki generation, publication, or other external services
without separately considering credentials, cost, and source-data disclosure.

## Serena: Python symbols and references

`.serena/project.yml` selects the Python LSP backend. It includes application
code and tests, respects `.gitignore`, and excludes environment files, caches,
build output, and the GitNexus index. Only this checkout is a workspace folder.

Pre-index or refresh after significant code changes:

```sh
uvx --python 3.13 --from serena-agent==2.1.0 serena project index .
```

Check the language backend when navigation fails:

```sh
uvx --python 3.13 --from serena-agent==2.1.0 serena project health-check .
```

When using MCP, confirm the active project first (`get_current_config` and,
if necessary, `activate_project`). Use `get_symbols_overview`, `find_symbol`, and
`find_referencing_symbols` for Python navigation. Before changing a signature or
renaming a symbol, inspect its references. Prefer the IDE's Python LSP when it
already provides the needed operation. Use `rg` for `.bean` syntax, configuration,
string-based plugin names, and documentation.

Serena's local symbol cache, generated logs, optional memories, and
`project.local.yml` overrides are ignored by Git. Memories are convenience notes, not authoritative behavior
or release requirements; keep those in the existing documentation and tests.

## GitNexus: graph context and impact

Create or refresh the local index from the repository root:

```sh
npm exec --yes --package=gitnexus@1.6.12 -- gitnexus analyze --index-only --name cashier-server-python
npm exec --yes --package=gitnexus@1.6.12 -- gitnexus status
```

`--index-only` prevents generated AGENTS/CLAUDE instructions and skills from
replacing the project's deliberately scoped rules. Do not use `--self-commit`.
The local `.gitnexus/` index is ignored by Git; GitNexus also registers this
checkout in its user-level registry. `.gitnexusignore` excludes secrets and
artifacts while keeping Python tests available. Built-in exclusions are not
broadly overridden: fixture directories can remain outside the graph, and
checked-in Beancount fixtures remain available through `rg` and tests.
Embeddings are not enabled.

CLI fallbacks, also available through GitNexus MCP:

```sh
npm exec --yes --package=gitnexus@1.6.12 -- gitnexus context StandaloneSnapshotBuilder --repo cashier-server-python
npm exec --yes --package=gitnexus@1.6.12 -- gitnexus impact classify_source_entries --direction upstream --repo cashier-server-python
npm exec --yes --package=gitnexus@1.6.12 -- gitnexus detect-changes --scope all --repo cashier-server-python
```

For a nontrivial cross-file change, check context/impact before editing when the
index is available, then refresh and inspect graph changes after editing. CLI
exit status alone is not proof: inspect warnings, freshness, risk, and partial
or truncated output. Empty callers, `UNKNOWN`, and missing edges are inconclusive;
confirm with Python LSP, `rg`, and relevant tests. Configuration-only changes can
legitimately have no affected graph symbols.

Do not copy another checkout's index or `.gitnexus/run.cjs`: indexes contain
checkout-specific paths and generated runners can select an unpinned version.
Use the pinned commands above in a fresh clone and after switching branches.

## Safety boundaries and validation

Index **only this source repository**, never the production ledger workspace,
`/workspace`, or a parent directory containing private financial data. Do not add
production ledger paths as Serena workspace folders or GitNexus repositories.
Credentials and production books must not be stored in either index or memory.
GitNexus's default local index can retain source text, so treat it as local data.

A graph of Python calls does not establish:

- execution of plugins selected dynamically by Beancount source;
- accounting equivalence of `@`, `@@`, cost annotations, or booking;
- include/glob and filesystem mount behavior;
- writeback durability, idempotency, or multi-process safety;
- compatibility across the HTTP/PWA/RustLedger boundary.

Run the relevant tests with `uv run pytest`. Docker/RustLedger acceptance is
opt-in and is not proved by a normal test run; see `docs/releasing.md` and
`tests/test_container_acceptance.py`. Keep development indexes out of Docker
build context (`.dockerignore`) and never infer release readiness from a successful
index or MCP startup.
