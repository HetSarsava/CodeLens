# CodeLens

CodeLens helps coding agents review Python changes over MCP. It traces callers, shows which pytest cases executed changed lines, and checks whether those tests catch a plausible regression.

## A bug that passed 38 tests

In a live run against a clone of Python's `humanize` library, Codex added an option to trim fractional zeros. It proposed a regression that only trimmed when the caller supplied a precision. All 38 selected tests still passed, even though `1200.0` formatted as `"1,200.0"` where the feature called for `"1,200"`. CodeLens verified a new assertion on both versions. After Codex added it, the correct version passed all 39 cases and the same mutation failed both reruns. [Experiment and results](docs/adaptive-mutation.md#observed-results)

## Try it locally

Requires Git, Python 3.12 or later, and [uv](https://docs.astral.sh/uv/).

```sh
git clone https://github.com/HetSarsava/CodeLens.git
cd CodeLens
uv sync --frozen
uv run --frozen codelens review-demo --output logs/review-demo
```

Open `logs/review-demo/weak/.codelens/review/report.html` and the matching report under `strong`. The demo compares two real pytest runs on a sample checkout function. The weak tests miss an arithmetic mutation and the strong tests detect it. Pick a new output directory each time.

For a dependency map, run `uv run --frozen codelens tree tests/fixtures/sample_repo` and open the HTML file it prints.

## Connect a coding agent

CodeLens runs as a local stdio MCP server, with no API key required by the server. It binds to one Python repository.

| Workflow | What CodeLens returns | Guide |
|---|---|---|
| Inspect impact | Source locations, direct and transitive callers, and unresolved call counts | [MCP setup and graph tools](docs/mcp-guide.md) |
| Review a patch | Per-test coverage of changed lines, bounded mutation results, and HTML and JSON reports | [Patch review](docs/change-review.md) |
| Verify a regression test | A reproduced assertion failure through an agent-proposed mutation in Docker | [Adaptive mutation testing](docs/adaptive-mutation.md) |

Patch review executes trusted project tests using the local interpreter and disposable source copies. Adaptive mutation testing runs pytest in network-disabled Linux containers and requires Docker. Each finding describes the scope of the executed tests and the exact change it checked. [Architecture](docs/architecture.md)

## Development

```sh
uv sync --frozen --extra dev
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen mypy src/codelens
uv run --frozen pytest -q
```

Windows and Linux CI exercise the real MCP subprocess. The Linux job also builds the test image and checks the Docker execution boundary.
