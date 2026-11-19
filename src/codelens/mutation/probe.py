"""Standalone entry point inside the runner image. Requires only pytest and stdlib."""

import ast
import json
import os
import shutil
import sys
import time
from pathlib import Path

import pytest


class Recorder:
    def __init__(self):
        self.collected = []
        self.cases = {}
        self.reason = ""
        self.assertions = {}

    def pytest_collection_finish(self, session):
        if len(session.items) > 256:
            pytest.exit("Scope exceeds 256 cases; narrow the test paths.", returncode=4)
        self.collected = [item.nodeid for item in session.items]

    def pytest_collectreport(self, report):
        if report.failed:
            self.reason = "collection: " + str(report.longrepr)[-1500:]

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_makereport(self, item, call):
        outcome = yield
        report = outcome.get_result()
        report.codelens_assertion = bool(
            call.excinfo and call.excinfo.errisinstance(AssertionError)
        )

    def pytest_runtest_logreport(self, report):
        if report.when != "call" and report.passed:
            return
        case = {"node_id": report.nodeid, "phase": report.when, "outcome": report.outcome}
        if report.skipped and hasattr(report, "wasxfail"):
            case["outcome"] = "xfailed"
        if report.failed:
            case["evidence"] = str(report.longrepr)[-1200:]
            crash = getattr(report.longrepr, "reprcrash", None)
            if crash:
                path = Path(crash.path).resolve()
                root = Path.cwd().resolve()
                if path.is_relative_to(root):
                    name = path.relative_to(root).as_posix()
                    case.update(path=name, line=crash.lineno)
                    if name not in self.assertions:
                        self.assertions[name] = {
                            node.lineno
                            for node in ast.walk(ast.parse(path.read_bytes()))
                            if isinstance(node, ast.Assert)
                        }
                    case["assertion"] = (
                        getattr(report, "codelens_assertion", False)
                        and crash.lineno in self.assertions[name]
                        and name == report.nodeid.split("::")[0]
                    )
        self.cases[report.nodeid] = case


def main():
    source, work, *scope = sys.argv[1:]
    output = os.dup(sys.stdout.fileno())
    with open(os.devnull, "w") as sink:
        os.dup2(sink.fileno(), 1)
        os.dup2(sink.fileno(), 2)
    started = time.monotonic()
    shutil.copytree(source, work)
    os.chdir(work)
    sys.path[:0] = [str(Path.cwd()), str(Path.cwd() / "src")]
    recorder = Recorder()
    status = pytest.main(
        ["--rootdir=.", "-p", "no:cacheprovider", "--color=no", "--tb=short", "-q", "--", *scope],
        plugins=[recorder],
    )
    with os.fdopen(output, "w", encoding="utf-8") as stream:
        json.dump(
            {
                "scope": scope,
                "exit_code": int(status),
                "collected": recorder.collected,
                "cases": list(recorder.cases.values()),
                "reason": recorder.reason,
                "seconds": time.monotonic() - started,
            },
            stream,
        )


if __name__ == "__main__":
    main()
