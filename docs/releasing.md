# Release policy

## Branches and versions

The owner chooses `X.Y.Z`; agents and automation do not infer it from commits. Use PATCH for compatible fixes, MINOR for compatible new behavior, and MAJOR for incompatible API, behavior, or persisted-data changes.

`main` is the production branch and the default base for a new release. Fetch `origin` and create `releases/X.Y.Z` from current `origin/main`, without tracking `origin/main` as its upstream. Normally keep one active release branch; there is no permanent `develop`.

| Purpose | Branch | PR destination |
| --- | --- | --- |
| Release integration | `releases/X.Y.Z` | `main` |
| Feature, fix, docs, maintenance, automation | `feat/…`, `fix/…`, `docs/…`, `chore/…`, `ci/…` | The target `releases/X.Y.Z` |

Create work branches from the target release branch. Squash-merge both work PRs into the release branch and the release PR into `main`; do not use merge commits. Review existing work branches by their full diff rather than recreating them solely to fit a naming convention. The release branch can be deleted after successful publication; published versions remain.

Keep these three version entries equal to `X.Y.Z` as reviewed release content:

- `[project].version` in `pyproject.toml`.
- The root `cashier-server` package version in `uv.lock`, identified by `source = { editable = "." }`.
- The literal `version` passed to `FastAPI(...)` in `main.py`, exposed in API/OpenAPI metadata.

The top-level `uv.lock` version describes the lockfile format. Dependency versions, including RustLedger, are independent of the server version. Validation rejects mismatches; it does not rewrite metadata or choose another version. `scripts/release_version.py` reads TOML and the Python AST without importing application code.

| Output | Name |
| --- | --- |
| RC image | `ghcr.io/sirily/cashier-server-python:X.Y.Z-rc.N` |
| Final image | `ghcr.io/sirily/cashier-server-python:X.Y.Z` |
| Final Git tag and GitHub Release | `vX.Y.Z` |
| Moving final-image alias | `ghcr.io/sirily/cashier-server-python:latest` |

## Release sequence

### 1. Integrate work and publish candidates

Each push to `releases/X.Y.Z` triggers `.github/workflows/docker.yml`. Manual dispatch on that release branch follows the same path. The workflow validates the branch and metadata, runs shared checks, and publishes a `linux/amd64` and `linux/arm64` image.

`N` is assigned automatically from `github.run_number`, not `github.run_attempt`. The counter belongs to the workflow, not an individual version: gaps are normal and the first candidate need not be `rc.1`. Rerunning the same run retains its number; a new run receives another number. Further pushes continue the same development process without a freeze or separate candidate-preparation step.

### 2. Squash-merge the release PR

Open a same-repository PR from `releases/X.Y.Z` into `main`; it can remain open while work accumulates. Review the changes and CI results for the current integration before merging.

**Squash-merging this release PR is release approval.** The resulting squash commit is the final release source; no additional environment approval, acceptance receipt, or test-deployment URL is part of the process.

### 3. Publish from the exact current-main squash commit

`.github/workflows/publish.yml` runs on `main` pushes and manual dispatch. Final publication requires all of the following:

- The event is a push or dispatch on `refs/heads/main`, and its full `GITHUB_SHA` equals the current remote `main` head.
- GitHub's PRs associated with that commit include a merged PR from `releases/X.Y.Z` to `main`, with both head and base repositories equal to this repository.
- That PR's `merge_commit_sha` equals the event SHA exactly. The release-branch head SHA is not the squash SHA and is not used as a substitute.
- The release-branch version matches all three metadata entries, and the shared checks pass again for the squash commit.

An arbitrary `main` push, work-branch run, fork PR, or manually pushed tag does not qualify as a final release.

Publication builds a **fresh final image from the squash commit**, not by promoting or retagging an RC. The final digest can differ from a candidate digest. After checking the final multi-platform image and its provenance, automation:

1. Creates `vX.Y.Z` on the exact squash commit and creates the GitHub Release with source SHA, image digest, and workflow-run URL. A new release is initially created with `--latest=false`.
2. Rechecks current `main`, updates the container `latest` alias from the verified final image-index digest, and verifies that the alias resolves to that same digest.
3. Marks the corresponding GitHub Release **Latest**, including when completing an interrupted publication with an existing release.

## Required checks and audit reporting

`.github/workflows/checks.yml` is shared by PR CI and both publication workflows. Its `Required checks` job requires every check job to succeed. Checks with satisfied prerequisites continue after another check fails; a failure remains blocking.

| Gate | Coverage |
| --- | --- |
| Workflow syntax | Checksum-verified Actionlint 1.7.7 with ShellCheck; root workflows checked using an isolated empty configuration |
| Python 3.12 and 3.13 | Locked dependency installation, version validation, Ruff linting, Basedpyright type checking, Node release regression tests, and the Python test suite |
| Wheel | Build on both Python versions; verify application modules, package name/version, and the `cashier-server = main:main` entry point |
| Production platforms | Non-pushing Docker builds for both `linux/amd64` and `linux/arm64` |
| Container acceptance | Build the production Dockerfile; exercise authentication, runtime plugins, read-only ledger fixtures, and RustLedger snapshot parsing |
| Dependency audit | Python runtime/dev/build dependencies, Node acceptance dependencies, and the production container image |

The Python jobs use `UV_PYTHON` to select each matrix interpreter and run:

```sh
uv python install
uv sync --locked
node scripts/release.mjs validate-version
node --test scripts/release-tests.mjs
uv run --locked ruff check .
uv run --locked basedpyright --warnings
uv run --locked pytest --timeout=180 -v
uv build --wheel
```

The container job uses Python 3.13, the locked Node acceptance dependencies, Docker, and pinned Trivy 0.76.2:

```sh
uv sync --locked
npm --prefix tests/acceptance ci
docker build --tag cashier-auth-server:e2e .
DOCKER_ACCEPTANCE=true CASHIER_ACCEPTANCE_SKIP_BUILD=true uv run --locked pytest tests/test_container_acceptance.py --timeout=180 -v
uv run --locked python scripts/audit.py --image cashier-auth-server:e2e
```

The skip-build override reuses the image built immediately beforehand from the same source. Without the override, container acceptance builds its own image. A normal Python test run skips container acceptance unless `DOCKER_ACCEPTANCE=true` is set.

Acceptance requires `@rustledger/wasm@0.14.1` to parse the standalone snapshot with **0 parse errors**. Preserve source-preserving export, Python plugin materialization, include/glob behavior, and RustLedger boundary coverage. Python tests also cover manual-transaction validation, `cashier_id` deduplication/conflicts, partial success, and single-file writeback constraints.

`scripts/audit.py` runs pip-audit, npm audit, and Trivy. Findings at every severity, including high and critical, are advisory: retain their original severity and full report details in CI logs and the Actions run summary (`GITHUB_STEP_SUMMARY`), and emit Actions notices. No separate downloadable report artifacts are created. Disclose unauditable dependencies and coverage limitations rather than presenting them as clean results.

Scanner/tool, network, authentication, timeout, malformed-report, inconsistent-exit, and summary-reporting failures block the audit and publication. Successful auditing means valid completed scans, not zero findings. Missing prerequisites or skipped required checks are not passing gates.

## Immutability, retries, and protections

Versioned image tags, including RCs, and final Git tags are immutable. OCI annotations and labels record the source repository, full source SHA, and image version. Before publishing or reusing an image, `image-status` binds the release SHA to the workflow commit, validates the release event/ref and RC run number, and checks index annotations and both required platforms. New images receive GitHub build-provenance attestations bound to their exact digest. Verification requires the matching source commit/ref and the repository's RC or final publisher workflow on a GitHub-hosted runner. Matching signed images are reused, not rebuilt over; collisions, missing/invalid attestations, and unverifiable registry responses fail closed. Compare multi-platform **image-index digests**, not single-platform manifest digests.

An interrupted final run resumes only for the same eligible current-main commit and matching version. An existing Git tag must point directly to that commit; a missing final image behind an existing tag is an error. A newer final Git version blocks completion of an older version. Final publication is serialized without cancelling an in-progress run; stale runs cannot advance `latest`. Retry using the existing Actions run rather than editing tags or replacing images.

Use an active Git tag ruleset targeting `v*`: enable **Restrict updates** and **Restrict deletions**, leave **Restrict creations** disabled, and keep the bypass list empty. This permits automation to create new tags without moving or deleting existing ones. Git tag rules do not protect GHCR tags; restrict registry write access separately and retain the publisher's collision/reuse safeguards. Do not manually assign RC numbers, create release tags, or bypass the PR/publication sequence.

All workflows default to `contents: read`. Grant additional permissions only to the jobs that use them:

| Job | Permissions |
| --- | --- |
| PR/shared checks | `contents: read` |
| RC publication | `contents: read`, `packages: write`, `id-token: write`, `attestations: write` |
| Final planning | `contents: read`, `pull-requests: read` |
| Final publication | `contents: write`, `packages: write`, `pull-requests: read`, `id-token: write`, `attestations: write` |

Keep registry and repository write credentials out of untrusted PR checks. Checkouts do not persist credentials.

## Workflow and script map

| File | Responsibility |
| --- | --- |
| `.github/workflows/ci.yml` | PR and manual-dispatch entry point for shared checks |
| `.github/workflows/checks.yml` | Workflow syntax, Python matrix, wheel verification, container/RustLedger acceptance, audits, and aggregate gate |
| `.github/workflows/docker.yml` | Release-branch RC planning, checks, multi-platform publication, and digest verification |
| `.github/workflows/publish.yml` | Current-main final planning, checks, serialized final publication, and release completion |
| `scripts/release.mjs` | `validate-version`, `plan-rc`, `plan-final`, `image-status`, and `finish-final` commands, invoked as `node scripts/release.mjs <command>` |
| `scripts/release_version.py` | Package, lockfile, and FastAPI metadata validation, called through `uv` by the release driver |
| `scripts/audit.py` | Read-only audits, report validation, advisory findings, blocking infrastructure errors, and log/summary reporting |
| `scripts/release-tests.mjs` | Release planning, provenance, immutability, retry, and Latest-ordering regression tests |

Run `node --test scripts/release-tests.mjs` before changing release orchestration and `uv run --locked pytest tests/test_release_audit.py` before changing audit behavior.

## Deployment, hotfixes, and rollback

Publishing does not deploy. Deployment is a separate operation using an explicit final version, preferably pinned by digest, never a floating `latest` reference.

A hotfix follows the same process: open an owner-selected patch release from current `main`, publish candidates, and squash-merge its release PR. Integrate the released hotfix into any other active release branch before its final merge.

Rollback deploys an earlier immutable final image digest; it does not move tags, rewrite `main`, or undo ledger data. Check PWA/API, plugin, Beancount-source, and persisted-data compatibility first. Keep the ledger workspace read-only, with only the fixed `manual_transactions.bean` file writable for manual transaction writeback.
