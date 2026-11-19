from collections.abc import Callable
from pathlib import Path
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field, JsonValue

from .models import Key, Limits, Proposal, Result, Scope, TestPatch
from .service import Service


def register(server: MCPServer, root: Path, require_execution: Callable[[], None]) -> None:
    service = Service(root)
    annotations = ToolAnnotations(
        read_only_hint=False, destructive_hint=False, open_world_hint=False
    )

    @server.tool(annotations=annotations)
    def prepare_mutation_run(
        idempotency_key: Key,
        test_paths: Scope,
        focus: Annotated[list[Key], Field(max_length=32)] | None = None,
        requirement: Annotated[str, Field(max_length=2000)] = "",
        limits: Limits | None = None,
    ) -> Result:
        """Freeze this Git workspace and establish a passing isolated pytest baseline.
        Focus accepts file paths or graph symbol IDs. Poll pending operations with get_mutation_run.
        """
        require_execution()
        return service.submit(
            "prepare",
            idempotency_key,
            {
                "test_paths": test_paths,
                "focus": focus or [],
                "requirement": requirement,
                "limits": (limits or Limits()).model_dump(),
            },
        )

    @server.tool(annotations=annotations)
    def run_mutation_batch(
        run_id: Key,
        idempotency_key: Key,
        proposals: Annotated[list[Proposal | JsonValue], Field(min_length=1, max_length=16)],
    ) -> Result:
        """Test independent expression replacements. Per item supply target_id, expected_anchor,
        replacement, rationale and optional distinguishing_input or retry_inconclusive. One bad
        item leaves siblings runnable. A killed mutant is a successful tool invocation.
        """
        require_execution()
        items = [p.model_dump() if isinstance(p, Proposal) else p for p in proposals]
        return service.submit("batch", idempotency_key, {"proposals": items}, run_id)

    @server.tool(annotations=annotations)
    def verify_regression_test(
        run_id: Key,
        mutant_id: Key,
        idempotency_key: Key,
        patch: TestPatch,
        test_ids: Scope,
        assertion_line: Annotated[int, Field(ge=1)],
    ) -> Result:
        """Verify exact UTF-8 test file content on the feature and planted bug. Use exact node IDs
        and a one-based assert line. Supply before_sha256 for existing files, null for a new file.
        """
        require_execution()
        return service.submit(
            "verify",
            idempotency_key,
            {
                "mutant_id": mutant_id,
                "patch": patch.model_dump(),
                "test_ids": test_ids,
                "assertion_line": assertion_line,
            },
            run_id,
        )

    @server.tool(annotations=annotations)
    def get_mutation_run(
        run_id: Key | None = None,
        operation_id: Key | None = None,
        target: Key | None = None,
        offset: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=32)] = 12,
        cancel: bool = False,
    ) -> Result:
        """Poll or cancel work, recover results, or page anchors/findings. Prefer operation_id
        for one call; run_id includes earlier findings. Test scope is always explicit.
        """
        require_execution()
        return service.get(run_id or "", operation_id or "", offset, limit, target or "", cancel)
