# CodeLens architecture

The MCP server exposes three connected capabilities for one local Python repository.

| Package | Responsibility |
|---|---|
| `graph` | Parse Python with Tree-sitter, resolve recognizable call sites, and export a standalone HTML tree. Unknown call targets remain visible as unresolved edges. |
| `mcp_server` | Register typed MCP tools with the Python SDK and cache one read-only graph in memory. A hash of Python file paths and contents invalidates the graph after edits. |
| `review` | Compare a Git base with the working tree, connect changed lines to enclosing symbols, attribute coverage to pytest cases, and render bounded mutation evidence. |
| `mutation` | Freeze a Git working-tree baseline, validate and apply agent-proposed expression changes with LibCST, and compare pytest observations in separate Docker runs. |

## Review flow

The caller chooses a Git base and a pytest scope. Tree-sitter maps changed lines and assembles partial caller paths. Coverage.py records executable line contexts for each test. The review engine runs eligible built-in mutations under a fixed budget and writes the report as HTML and JSON. Review tests run in disposable copies under the host interpreter. Host pytest can access local files and the network as the current user. Use patch review on trusted projects.

## Adaptive mutation flow

The coding agent proposes a targeted expression replacement. CodeLens validates the target against the frozen snapshot, applies the change with LibCST, and reruns the same test selection in a network-disabled Linux container. A surviving mutation can lead the agent to draft a test. CodeLens verifies that the exact draft passes on the original snapshot and fails at its intended assertion on the mutant before the agent decides whether to save it.

SQLite records operations so a client can poll, recover results, or cancel. Mutations and candidate tests execute in disposable copies. Only the client adopts a verified test in the working tree.

[Review semantics and limits](change-review.md) · [Adaptive tool contract](adaptive-mutation.md)
