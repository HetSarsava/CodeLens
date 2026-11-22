# Connect CodeLens over MCP

A coding host launches CodeLens as a local subprocess. The official Python MCP SDK handles tool discovery, input validation, structured results, and JSON-RPC over stdio. The server is bound to the repository selected at startup.

## Test the connection

From the CodeLens checkout:

```sh
uv sync --frozen
uv run --frozen python examples/mcp_client.py tests/fixtures/sample_repo --name add
```

The example starts an actual server, discovers its ten tools, and calls `blast_radius` on the sample function. It prints JSON that labels direct and transitive callers. For a coding host that accepts an `mcpServers` object:

```json
{
  "mcpServers": {
    "codelens": {
      "command": "uv",
      "args": [
        "--directory", "/absolute/path/to/CodeLens",
        "run", "--frozen", "codelens-mcp",
        "--repo", "/absolute/path/to/python-repo"
      ]
    }
  }
}
```

Use absolute paths. Forward slashes work in Windows JSON paths, for example `C:/Projects/CodeLens`. Add `--allow-execution` to enable the review and mutation execution tools for a trusted repository. Follow the [adaptive setup](adaptive-mutation.md) to build the Docker image and register with Codex.

## Tools

| Tool | Use |
|---|---|
| `index_repo` | Get counts for symbols, edges, unresolved calls, and files. |
| `find_definition` | Find source locations for a name. |
| `find_callers` | List direct callers of a resolved symbol. |
| `blast_radius` | List direct and transitive callers of a function or method. |
| `review_patch` | Collect execution evidence on a patch and return focused findings. |
| `read_review_report` | Read the latest patch report and check whether it matches current source. |
| `prepare_mutation_run` | Freeze a snapshot, run a test baseline, and return mutation targets. |
| `run_mutation_batch` | Validate agent-proposed edits and test each mutant independently. |
| `verify_regression_test` | Check a test draft on the original and mutated versions. |
| `get_mutation_run` | Poll or cancel an operation and retrieve findings. |

For impact questions, start with `blast_radius` and pass the function name directly:

```json
{"name":"blast_radius","arguments":{"name":"calculate_total","max_depth":3}}
```

Include the repository-relative `file_path` if the name is ambiguous. Follow `next_offset` to fetch more symbols. The index is reused while Python source is unchanged. Dynamic calls and unresolved edges limit the results.

The integration tests cover tool schemas, discovery, queries, execution gating, and JSON-RPC interoperability. The adaptive mutation tests additionally execute a real client/server workflow from baseline through mutant verification.
