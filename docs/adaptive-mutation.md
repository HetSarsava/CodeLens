# Adaptive mutation testing over MCP

Ask your coding agent once:

> Update the profile page so display names are trimmed before rendering. If the name is empty after trimming, show “Guest”. Add tests and use CodeLens mutation testing to check that they catch a realistic regression.

The agent writes the feature and proposes specific mistakes. CodeLens freezes the working tree, runs pytest, and returns the observations. The agent decides whether another experiment would add evidence. Ordinary MCP stdio tools handle the entire exchange. No model credentials, sampling support or conversion of unittest tests are required by the server.

## Set up a client

From the CodeLens checkout, install the server and build its Linux test image:

```sh
uv sync --frozen --extra dev
docker build -t codelens-mutation:local -f docker/mutation.Dockerfile docker
codex mcp add codelens -- uv --directory /absolute/path/to/CodeLens run --frozen codelens-mcp --repo /absolute/path/to/project --allow-execution
```

Start a new Codex session in the **target project**. Other clients can launch the same command after the first `--` as a stdio MCP server. Configure one trusted Git working tree with at least one commit per server. The execution flag also enables the existing trusted-workspace tools. The four adaptive tools use Docker for all project and draft test execution.

The base image supplies CPython 3.12 and pytest. For projects with dependencies, derive a trusted image from `codelens-mutation:local`, install the pinned test requirements at image build time, and set `CODELENS_MUTATION_IMAGE` in the **server's** environment. Keep the checkout out of the image and avoid editable installs referring to host paths. CodeLens pins each run to the resolved image ID. Use a local Linux-container engine with bind mounting enabled. Tests that require network access, services, or writable host data need a different test scope.

## Client workflow

1. Implement the feature and initial tests in the real working tree. Identify observable behaviors separately. Call `prepare_mutation_run` with scoped pytest paths or node IDs and relevant files or symbol IDs. Poll if pending. Read the exact baseline cases, source anchors and changed lines.
2. For each behavior, propose one realistic mistake the user would notice. Copy the issued target ID and expected anchor. Include the rationale, distinguishing input and intended observation. Submit any number of batches within budget. Fix invalid proposals individually. Static callers identify possible paths. Only executed tests supply evidence.
3. A reproducibly killed mutant may finish that behavior. Investigate a meaningful survivor by drafting a focused assertion. Send the **exact test module** to `verify_regression_test`. For an existing module, retain its existing content and supply its original SHA-256. A candidate that passes twice needs a different input, assertion or execution path. Use a new key for a revised draft. For flaky or infrastructure evidence, use at most one justified retry.
4. Before adopting a verified draft, check freshness, the reported hash and assertion evidence. Save the exact UTF-8 bytes in the target project and run its normal suite. Saving a test changes the baseline. Prepare again before making fresh claims about that tree. Stop when behaviors have evidence, the budget expires or another attempt adds little. Summarize exact test scope, protected behavior and unresolved cases. Claim rendered output only when the test inspects rendered output.

## Tool contract

Discover machine-readable input and output schemas via `tools/list`. Successful calls return a Pydantic result through the SDK's `structuredContent`; clients can also read the text content. Tool-level errors cover invalid envelopes, conflicting idempotency keys, disabled execution and unknown runs. Test failures and killed mutants are data.

| Tool | Inputs | Evidence |
|---|---|---|
| `prepare_mutation_run` | `idempotency_key`, `test_paths`, optional `focus`, `requirement`, `limits` | `run_id`, `operation_id`, `baseline_fingerprint`, image ID, exact scope, limits, baseline collected IDs and per-case outcomes, diff excerpt, targets |
| `run_mutation_batch` | `run_id`, `idempotency_key`, `proposals[]` with `target_id`, `expected_anchor`, `replacement`, `rationale`, optional `distinguishing_input`, `retry_inconclusive` | One finding per processed proposal in input order, mutant ID, applied diff, observations, reason and next action |
| `verify_regression_test` | `run_id`, `mutant_id`, `idempotency_key`, `patch: {path, before_sha256, content}`, exact `test_ids`, one-based `assertion_line` | Content SHA-256, observations for both versions and confirmation for a reproduced failure |
| `get_mutation_run` | `run_id` for history or `operation_id` for one request; optional `offset`, `limit`, `target`, `cancel` | State, partial/completed findings, remaining budget, pending operations, next offset; filter by symbol, source file, target or mutant ID |

An observation contains `scope`, `exit_code`, `collected`, `cases`, `seconds` and `reason`. Each case includes its node ID, phase, outcome, failure location and bounded assertion report when available. Baseline skips are visible. Kills require the same collection and a repeatable call-phase failure of a previously passing test. Setup, teardown, collection, timeout and inconsistent observations are inconclusive. Survival applies only to the configured scope. Verification requires every requested candidate ID to pass on the original and the intended assertion to fail twice on the mutant. It establishes evidence about that specific edit and example.

Poll pending operations with their ID. Replaying an identical request with its original key returns the same operation. A new key with an identical mutation returns a duplicate finding; a prior inconclusive mutation permits an explicit retry. One active job runs per workspace. Completed findings and frozen inputs survive MCP reconnects in a private temporary directory. An interrupted worker's lease expires by the run deadline plus cleanup time. Old runs expire on access after 24 hours.

## Scope and limits

The snapshot uses Git's tracked and nonignored working-tree files, including staged/unstaged edits, untracked tests and deletions. Git metadata, tool caches and virtual environments are excluded. The adaptive workflow accepts regular files, UTF-8 Python source and a bounded Git workspace (10,000 files, 4 MiB/file, 48 MiB total). It rejects links. The symbol graph supplies enclosing functions and static caller hints. This workflow does not collect line coverage.

Choose a single-line assignment value, return value or if-condition in an issued function/method target. Replacement expressions support local names, literals, comparisons, boolean operators, conditional expressions and addition/subtraction. LibCST edits precisely one location. Test drafts support one new or existing `tests/**/test_*.py` module at a time. The targeted assertion must be a Python `assert` in that module. Existing pytest-collectable unittest tests can participate in the baseline.

Each observation runs in a fresh copy inside an unprivileged container with a read-only host input, a private writable tmpfs, no network and bounded CPU, memory, process count, time and report size. There is no host-run fallback. Defaults are eight candidate experiments, 30 seconds per pytest run and a 300-second total window including time between client calls. Maximums are 32, 120 and 900. Candidate budget covers each accepted mutation or draft; confirmation runs are included. The server returns up to 256 targets and accepts up to 256 collected tests per scope.

## Reproduce the evidence

```sh
# Real client/server stdio and real pytest on an authored disposable fixture
uv run --frozen pytest -q tests/test_adaptive_mutation.py
# Same workflow using the production Docker backend (PowerShell: $env:CODELENS_TEST_DOCKER='1')
CODELENS_TEST_DOCKER=1 uv run --frozen pytest -q tests/test_adaptive_mutation.py
```

The default developer test substitutes only container transport for this authored fixture. Linux CI builds the image and selects Docker. The fixture renders HTML through the production function, exercises two different regressions, adopts the verified tests and runs the resulting suite. Its second run starts with adequate tests and needs only preparation and one batch.

For a separate **live model demonstration**, register the server against a disposable Git project with a profile-rendering entry point, run the one-prompt example from that project, and capture the actual calls and delivered diff:

```sh
codex exec --sandbox workspace-write --json "Update the profile page so display names are trimmed before rendering. If the name is empty after trimming, show 'Guest'. Add tests and use CodeLens mutation testing to check that they catch a realistic regression." > ../profile-agent.jsonl
git diff HEAD
git ls-files --others --exclude-standard
```

Keep the transcript outside the target tree so it does not invalidate the frozen baseline.
