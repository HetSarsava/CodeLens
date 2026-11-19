"""Real stdio + pytest. CI selects production Docker; local tests use only this authored fixture."""

import asyncio
import hashlib
import os
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters, stdio_client

from codelens.mcp_server.server import create_server
from codelens.mutation import probe
from codelens.mutation.models import Observation, Result
from codelens.mutation.runner import DockerRunner
from codelens.mutation.service import Service
from codelens.review.changes import git


class FixtureRunner(DockerRunner):
    """Test double for container transport. Production has no switch for this backend."""

    def image(self):
        return "fixture-python-" + sys.version

    def run(self, source, scope, image, timeout, cancelled):
        with tempfile.TemporaryDirectory() as tmp:
            command = [
                sys.executable,
                "-I",
                probe.__file__,
                str(source),
                str(Path(tmp) / "work"),
                *scope,
            ]
            try:
                result = subprocess.run(command, capture_output=True, timeout=timeout, check=True)
            except subprocess.TimeoutExpired:
                return Observation(scope=scope, reason="timeout")
            return Observation.model_validate_json(result.stdout)


@asynccontextmanager
async def connected(root):
    if os.environ.get("CODELENS_TEST_DOCKER") == "1":
        args = ["-m", "codelens.mcp_server.server", "--repo", str(root), "--allow-execution"]
    else:
        args = ["-m", "tests.test_adaptive_mutation", str(root)]
    async with Client(
        stdio_client(StdioServerParameters(command=sys.executable, args=args)),
        read_timeout_seconds=30,
    ) as client:
        yield client


async def call(client, tool, **args):
    reply = await client.call_tool(tool, args)
    assert not reply.is_error, reply
    assert reply.structured_content is not None
    return Result.model_validate(reply.structured_content)


async def wait(client, result):
    for _ in range(1200):
        if result.state != "pending":
            return result
        await asyncio.sleep(0.15)
        result = await call(client, "get_mutation_run", operation_id=result.operation_id)
    pytest.fail("Operation did not finish")


SOURCE = """from html import escape

def render_profile(name):
    display_name = name.strip()
    label = display_name or "Guest"
    return f"<h1>{escape(label)}</h1>"
"""
TESTS = """from views import render_profile

def test_name():
    assert render_profile("Alice") == "<h1>Alice</h1>"

def test_empty():
    assert render_profile("") == "<h1>Guest</h1>"
"""
MODULE = "tests/test_views.py"


@pytest.fixture
def profile(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "views.py").write_text("def render_profile(name): return name\n")
    (repo / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n")
    git(repo, "add", ".")
    git(
        repo,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-qm",
        "base",
    )
    (repo / "views.py").write_bytes(SOURCE.encode())
    (repo / "notes.txt").write_bytes(b"work in progress")
    git(repo, "add", "notes.txt")
    (repo / "tests").mkdir()
    (repo / MODULE).write_bytes(TESTS.encode())
    return repo


async def prepare(client, key="start", **kwargs):
    return await wait(
        client,
        await call(
            client,
            "prepare_mutation_run",
            idempotency_key=key,
            test_paths=["tests"],
            focus=["views.py"],
            requirement="Trim displayed names and render Guest for blank names",
            **kwargs,
        ),
    )


def proposal(base, anchor, replacement):
    t = next(t for t in base.targets if t.expected_anchor == anchor)
    return {
        "target_id": t.target_id,
        "expected_anchor": anchor,
        "replacement": replacement,
        "rationale": "Show the wrong name when the input contains extra whitespace",
        "distinguishing_input": "  Alice  ",
    }


def candidate(base, mid, key, content, path, line):
    return {
        "run_id": base.run_id,
        "mutant_id": mid,
        "idempotency_key": key,
        "patch": {
            "path": path,
            "content": content,
            "before_sha256": hashlib.sha256(TESTS.encode()).hexdigest() if path == MODULE else None,
        },
        "test_ids": [path + "::test_boundary"],
        "assertion_line": line,
    }


@pytest.mark.asyncio
async def test_adaptive_profile_workflow(profile):
    async with connected(profile) as client:
        base = await prepare(client)
        assert base.state == "ready", base.reason
        assert set(base.baseline.collected) == {MODULE + "::test_name", MODULE + "::test_empty"}
        trim = proposal(base, "name.strip()", "name")
        raw = proposal(base, 'display_name or "Guest"', 'name if display_name else "Guest"')
        args = {
            "run_id": base.run_id,
            "idempotency_key": "edits",
            "proposals": [
                dict(trim, expected_anchor="stale"),
                trim,
                dict(raw, replacement="name if"),
                raw,
                trim,
            ],
        }
        queued = await call(client, "run_mutation_batch", **args)
        retried = await call(client, "run_mutation_batch", **args)
        assert queued.operation_id == retried.operation_id
        batch = await wait(client, queued)
        assert [f.outcome for f in batch.findings] == [
            "invalid",
            "survived",
            "invalid",
            "survived",
            "duplicate",
        ]
        assert batch.remaining_candidates == 6
        assert all(f.diff for f in (batch.findings[1], batch.findings[3]))
        conflict = await client.call_tool("run_mutation_batch", dict(args, proposals=[trim]))
        assert conflict.is_error
    async with connected(profile) as client:
        recovered = await call(client, "get_mutation_run", operation_id=batch.operation_id)
        assert recovered.findings == batch.findings
        blanks = (
            TESTS + '\ndef test_boundary():\n    assert render_profile("   ") == "<h1>Guest</h1>"\n'
        )
        line = len(blanks.splitlines())
        trim_args = candidate(base, batch.findings[1].mutant_id, "spaces", blanks, MODULE, line)
        verdict = await wait(client, await call(client, "verify_regression_test", **trim_args))
        f = verdict.findings[0]
        assert f.outcome == "verified", f
        assert f.observations[1].cases[0].line == line and f.observations[1].cases[0].assertion
        raw_id = batch.findings[3].mutant_id
        weak = await wait(
            client,
            await call(
                client,
                "verify_regression_test",
                **dict(trim_args, mutant_id=raw_id, idempotency_key="weak"),
            ),
        )
        assert weak.findings[0].outcome == "undistinguished"
        padded = 'from views import render_profile\n\ndef test_boundary():\n    assert render_profile("  Alice  ") == "<h1>Alice</h1>"\n'
        path = "tests/test_padded.py"
        args = candidate(base, raw_id, "padded", padded, path, 4)
        stronger = await wait(client, await call(client, "verify_regression_test", **args))
        assert stronger.findings[0].outcome == "verified", stronger
        assert stronger.findings[0].patch_sha256 == hashlib.sha256(padded.encode()).hexdigest()
        assert (profile / MODULE).read_bytes() == TESTS.encode() and not (profile / path).exists()
        (profile / MODULE).write_bytes(blanks.encode())
        (profile / path).write_bytes(padded.encode())
        stale = await call(client, "get_mutation_run", run_id=base.run_id)
        assert stale.state == "stale"
        result = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "pytest", "-q", "tests"],
            cwd=profile,
            capture_output=True,
            check=True,
            text=True,
        )
        assert "4 passed" in result.stdout
        assert (profile / "views.py").read_bytes() == SOURCE.encode()
        assert git(profile, "show", ":notes.txt") == b"work in progress"

        # A second, shorter interaction: the adopted tests already catch both proposals.
        fresh = await prepare(client, "after-saving")
        short = await wait(
            client,
            await call(
                client,
                "run_mutation_batch",
                run_id=fresh.run_id,
                idempotency_key="already-covered",
                proposals=[
                    proposal(fresh, "name.strip()", "name"),
                    proposal(fresh, 'display_name or "Guest"', 'name if display_name else "Guest"'),
                ],
            ),
        )
        assert [f.outcome for f in short.findings] == ["killed", "killed"]
        assert all(len(f.observations) == 2 for f in short.findings)


@pytest.mark.asyncio
async def test_failed_candidates_and_recovery(profile):
    async with connected(profile) as client:
        base = await prepare(client)
        assert base.state == "ready", base
        batch = await wait(
            client,
            await call(
                client,
                "run_mutation_batch",
                run_id=base.run_id,
                idempotency_key="mutant",
                proposals=[proposal(base, "name.strip()", "name")],
            ),
        )
        mid = batch.findings[0].mutant_id
        bad = "def test_boundary():\n    assert False\n"
        args = candidate(base, mid, "both", bad, "tests/test_candidate.py", 2)
        both = await wait(client, await call(client, "verify_regression_test", **args))
        assert both.findings[0].outcome == "inconclusive"
        assert [o.exit_code for o in both.findings[0].observations] == [1, 1]
        wrong = 'from views import render_profile\n\ndef test_boundary():\n    assert render_profile("   ") == "<h1>Guest</h1>"\n    assert True\n'
        misplaced = candidate(base, mid, "wrong-assertion", wrong, "tests/test_candidate.py", 5)
        weak = await wait(client, await call(client, "verify_regression_test", **misplaced))
        assert weak.findings[0].outcome == "inconclusive"
        assert weak.findings[0].observations[1].cases[0].line == 4
        args["patch"]["path"] = "../escape.py"
        invalid = await wait(
            client,
            await call(client, "verify_regression_test", **dict(args, idempotency_key="bad-path")),
        )
        assert invalid.findings[0].outcome == "invalid"
        for i, (source, exit_code) in enumerate([("", 5), ("def broken(:", 2), (bad, 1)]):
            (profile / MODULE).write_bytes(source.encode())
            failed = await prepare(client, "failure" + str(i))
            assert failed.state == "baseline_failed" and failed.baseline.exit_code == exit_code
        (profile / MODULE).write_text("import time\ndef test_slow(): time.sleep(5)\n")
        slow = await prepare(client, "timeout", limits={"test_seconds": 1})
        assert slow.state == "baseline_failed" and slow.baseline.reason == "timeout"


def test_cancel_pending_operation_and_reconnect(profile):
    started = threading.Event()

    class WaitingRunner(FixtureRunner):
        def run(self, source, scope, image, timeout, cancelled):
            started.set()
            assert cancelled.wait(timeout)
            return Observation(scope=scope, reason="cancelled")

    service = Service(profile, WaitingRunner())
    request = {"test_paths": ["tests"], "focus": ["views.py"], "requirement": "", "limits": {}}
    first = service.submit("prepare", "first", request)
    assert started.wait(6)
    second_client = Service(profile)
    replay = second_client.submit("prepare", "first", request)
    assert replay.operation_id == first.operation_id
    busy = second_client.submit("prepare", "another", request)
    assert busy.state == "busy" and busy.operation_id == first.operation_id
    result = second_client.get(operation_id=first.operation_id, cancel=True)
    deadline = time.monotonic() + 6
    while result.state == "pending" and time.monotonic() < deadline:
        time.sleep(0.05)
        result = second_client.get(operation_id=first.operation_id)
    assert result.state == "cancelled"


@pytest.mark.skipif(
    os.environ.get("CODELENS_TEST_DOCKER") != "1", reason="Linux Docker isolation check"
)
@pytest.mark.asyncio
async def test_runner_boundary(profile):
    (profile / MODULE).write_text(
        "import os\nfrom pathlib import Path\n\n"
        "def test_environment():\n"
        "    assert os.getuid() == 65534\n"
        "    assert os.getcwd() == '/tmp/work'\n"
        "    assert set(os.listdir('/sys/class/net')) == {'lo'}\n"
        "    assert not os.access('/input/views.py', os.W_OK)\n"
        "    assert not Path('/var/run/docker.sock').exists()\n"
        "    assert 'PYTHONPATH' not in os.environ\n"
    )
    async with connected(profile) as client:
        result = await prepare(client)
        assert result.state == "ready", result
        assert result.baseline.cases[0].node_id == MODULE + "::test_environment"


def test_snapshot_tracks_working_bytes_and_deletions(profile, tmp_path):
    from codelens.mutation.workspace import snapshot

    (profile / "views.py").unlink()
    frozen = tmp_path / "copy"
    first = snapshot(profile, frozen)
    assert not (frozen / "views.py").exists()
    assert (frozen / MODULE).read_bytes() == TESTS.encode()
    (profile / MODULE).write_text("def test_new(): pass")
    assert snapshot(profile) != first
    assert (frozen / MODULE).read_bytes() == TESTS.encode()


def test_production_isolation_is_required(profile, monkeypatch):
    assert type(Service(profile).runner) is DockerRunner
    monkeypatch.setenv("PATH", "")
    with pytest.raises(ValueError, match="Docker runner unavailable"):
        Service(profile).runner.image()


if __name__ == "__main__":
    # Only this test-only entry point can run the authored fixture on host Python.
    from unittest.mock import patch

    with patch("codelens.mutation.service.DockerRunner", FixtureRunner):
        create_server(sys.argv[1], allow_execution=True).run(transport="stdio")
