"""Workspace-scoped durable jobs. Only the calling MCP client authors proposals/tests."""

import ast
import contextlib
import json
import shutil
import sqlite3
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .models import Finding, Limits, Observation, Outcome, Proposal, Result, TestPatch
from .runner import DockerRunner
from .workspace import context, digest, edit, relative, snapshot, validate_scope


def stable(value: Any) -> str:
    return digest(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def failures(obs: Observation) -> set[tuple[str, str, int]]:
    return {
        (c.node_id, c.path, c.line)
        for c in obs.cases
        if c.phase == "call" and c.outcome == "failed"
    }


def classify(base: Observation, obs: Observation) -> Outcome:
    if obs.reason or sorted(base.collected) != sorted(obs.collected):
        return "inconclusive"
    before = {c.node_id: c.outcome for c in base.cases}
    after = {c.node_id: c.outcome for c in obs.cases}
    if before.keys() != after.keys():
        return "inconclusive"
    if before == after and obs.green:
        return "survived"
    dead = {node for node, _, _ in failures(obs)}
    if (
        dead
        and obs.exit_code == 1
        and all(
            before[n] == "passed" and after[n] == "failed" if n in dead else before[n] == after[n]
            for n in before
        )
    ):
        return "killed"
    return "inconclusive"


class Service:
    def __init__(self, root: Path, runner: DockerRunner | None = None):
        self.root = root.resolve()
        self.store = Path(tempfile.gettempdir()) / (
            "codelens-mutation-" + digest(str(self.root).encode())[:24]
        )
        self.runner = runner or DockerRunner()

    @contextlib.contextmanager
    def db(self):
        if self.store.is_symlink() or self.store.is_junction():
            raise ValueError("Unsafe mutation store location")
        self.store.mkdir(mode=0o700, exist_ok=True)
        if hasattr(self.store.stat(), "st_uid"):
            import os

            if hasattr(os, "getuid") and (
                self.store.stat().st_uid != os.getuid() or self.store.stat().st_mode & 0o077
            ):
                raise ValueError("Mutation store must be private to the current user")
        con = sqlite3.connect(self.store / "runs.sqlite", timeout=10)
        con.row_factory = sqlite3.Row
        try:
            con.execute(
                "CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, created REAL, source TEXT, data TEXT)"
            )
            con.execute(
                "CREATE TABLE IF NOT EXISTS ops (id TEXT PRIMARY KEY, run TEXT, request TEXT, state TEXT, reason TEXT, findings TEXT, cancel INTEGER, lease REAL)"
            )
            con.execute("BEGIN IMMEDIATE")
            expired = con.execute(
                "SELECT id FROM runs WHERE created<?", (time.time() - 86400,)
            ).fetchall()
            for row in expired:
                shutil.rmtree(self.store / row["id"], ignore_errors=True)
                con.execute("DELETE FROM ops WHERE run=?", (row["id"],))
                con.execute("DELETE FROM runs WHERE id=?", (row["id"],))
            con.execute(
                "UPDATE ops SET state='inconclusive',reason='Worker interrupted or deadline expired; retry with a new key' WHERE state='pending' AND lease<?",
                (time.time(),),
            )
            yield con
            con.commit()
        finally:
            con.close()

    def submit(self, action: str, key: str, payload: dict, run_id: str = "") -> Result:
        op = stable([run_id or "prepare", key])
        request = stable([action, payload])
        start = False
        with self.db() as db:
            prior = db.execute("SELECT * FROM ops WHERE id=?", (op,)).fetchone()
            if prior:
                if prior["request"] != request:
                    raise ValueError("Conflicting reuse of idempotency_key; use a fresh key")
            else:
                active = db.execute("SELECT id,run FROM ops WHERE state='pending'").fetchone()
                if active:
                    return Result(
                        run_id=active["run"],
                        operation_id=active["id"],
                        pending_operations=[active["id"]],
                        state="busy",
                        next_action="Poll the active operation, then retry this request.",
                    )
                if action == "prepare":
                    run_id = uuid.uuid4().hex
                    limits = Limits.model_validate(payload["limits"])
                    result = Result(
                        run_id=run_id,
                        operation_id=op,
                        state="pending",
                        scope=payload["test_paths"],
                        requirement=payload["requirement"],
                        limits=limits,
                        remaining_candidates=limits.max_candidates,
                    )
                    db.execute(
                        "INSERT INTO runs VALUES (?,?,?,?)",
                        (run_id, time.time(), "", result.model_dump_json()),
                    )
                row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
                if not row:
                    raise ValueError("Unknown or expired run")
                deadline = (
                    row["created"] + Result.model_validate_json(row["data"]).limits.wall_seconds
                )
                db.execute(
                    "INSERT INTO ops VALUES (?,?,?,'pending','','[]',0,?)",
                    (op, run_id, request, min(time.time() + 15, max(time.time(), deadline) + 12)),
                )
                start = True
        if start:
            threading.Thread(
                target=self._work, args=(run_id, op, action, payload), daemon=True
            ).start()
        return self.get(operation_id=op, check_current=not start)

    def get(
        self,
        run_id: str = "",
        operation_id: str = "",
        offset: int = 0,
        limit: int = 12,
        target: str = "",
        cancel: bool = False,
        check_current: bool = True,
    ) -> Result:
        with self.db() as db:
            job = (
                db.execute("SELECT * FROM ops WHERE id=?", (operation_id,)).fetchone()
                if operation_id
                else db.execute(
                    "SELECT * FROM ops WHERE run=? ORDER BY rowid DESC LIMIT 1", (run_id,)
                ).fetchone()
            )
            if not job or (run_id and job["run"] != run_id):
                raise ValueError("Unknown or expired run/operation")
            row = db.execute("SELECT * FROM runs WHERE id=?", (job["run"],)).fetchone()
            result = Result.model_validate_json(row["data"])
            result.operation_id, result.state, result.reason = (
                job["id"],
                job["state"],
                job["reason"],
            )
            if cancel and job["state"] == "pending":
                db.execute("UPDATE ops SET cancel=1 WHERE id=?", (job["id"],))
            jobs = (
                [job]
                if operation_id
                else db.execute(
                    "SELECT * FROM ops WHERE run=? ORDER BY rowid", (job["run"],)
                ).fetchall()
            )
            result.findings = [
                Finding.model_validate(f) for j in jobs for f in json.loads(j["findings"])
            ]
            result.pending_operations = [j["id"] for j in jobs if j["state"] == "pending"]
            expected = row["source"]
            result.remaining_seconds = round(
                max(0, row["created"] + result.limits.wall_seconds - time.time()), 2
            )
        if check_current and expected and result.state != "pending":
            try:
                matches = snapshot(self.root) == expected
            except (ValueError, OSError) as exc:
                result.state, result.reason = "stale", f"Cannot confirm current workspace: {exc}"
            else:
                if not matches:
                    result.state, result.reason = (
                        "stale",
                        "Workspace changed since the captured baseline",
                    )
        if target:
            result.targets = [
                t for t in result.targets if target in (t.symbol, t.file, t.target_id)
            ]
            result.findings = [
                f
                for f in result.findings
                if f.mutant_id == target or f.proposal and f.proposal.target_id == target
            ]
        total = max(len(result.targets), len(result.findings))
        result.next_offset = offset + limit if offset + limit < total else None
        result.targets = result.targets[offset : offset + limit]
        result.findings = result.findings[offset : offset + limit]
        result.next_action = {
            "pending": "Poll get_mutation_run with operation_id; cancel=true stops it.",
            "baseline_failed": "Fix the baseline or runner setup, then prepare with a new key.",
            "stale": "Prepare a fresh baseline before using this evidence.",
            "exhausted": "Report the completed evidence and unresolved behaviors.",
            "ready": "Submit requirement-specific proposals for the issued targets.",
            "inconclusive": "Inspect diagnostics; retry with a new key if appropriate.",
        }.get(result.state, "Inspect the findings and remaining budget.")
        return result

    def _work(self, rid: str, op: str, action: str, payload: dict):
        state, reason = "inconclusive", ""
        stopped = threading.Event()
        done = threading.Event()
        with self.db() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (rid,)).fetchone()
            result = Result.model_validate_json(row["data"])
            deadline = row["created"] + result.limits.wall_seconds
            expected = row["source"]

        def watch():
            while not done.wait(0.25):
                with self.db() as db:
                    job = db.execute("SELECT cancel,state FROM ops WHERE id=?", (op,)).fetchone()
                    db.execute(
                        "UPDATE ops SET lease=? WHERE id=? AND state='pending'",
                        (min(time.time() + 15, deadline + 12), op),
                    )
                if not job or job["cancel"] or job["state"] != "pending" or time.time() >= deadline:
                    stopped.set()
                    return

        threading.Thread(target=watch, daemon=True).start()
        frozen = self.store / rid
        findings: list[Finding] = []

        def save():
            with self.db() as db:
                db.execute(
                    "UPDATE runs SET source=?,data=? WHERE id=?",
                    (expected, result.model_dump_json(), rid),
                )
                db.execute(
                    "UPDATE ops SET findings=? WHERE id=?",
                    (json.dumps([f.model_dump() for f in findings]), op),
                )

        def observe(folder: Path, scope: list[str]) -> Observation:
            seconds = min(deadline - time.time(), result.limits.test_seconds)
            if stopped.is_set() or seconds <= 0:
                return Observation(scope=scope, reason="cancelled or deadline reached")
            return self.runner.run(folder, scope, result.runner_image, seconds, stopped)

        try:
            if time.time() >= deadline:
                state, reason = "exhausted", "Run wall-clock budget expired"
            elif action == "prepare":
                # The enclosing store is private; the read-only mount must allow the container UID.
                frozen.mkdir(mode=0o755)
                expected = snapshot(self.root, frozen)
                validate_scope(frozen, result.scope)
                result.runner_image = self.runner.image()
                result.baseline_fingerprint = stable(
                    [
                        expected,
                        result.scope,
                        result.runner_image,
                        digest(Path(__file__).with_name("probe.py").read_bytes()),
                    ]
                )
                result.targets, result.diff_excerpt = context(
                    self.root, frozen, payload["focus"], result.baseline_fingerprint
                )
                result.baseline = observe(frozen, result.scope)
                state = "ready" if result.baseline.green else "baseline_failed"
                reason = result.baseline.reason or (
                    ""
                    if result.baseline.green
                    else "pytest must complete successfully with at least one passing test; inspect exit_code and cases"
                )
                save()
            elif not result.baseline or not result.baseline.green:
                state, reason = "baseline_failed", "Prepare a passing baseline first"
            elif snapshot(self.root) != expected:
                state, reason = "stale", "Workspace changed; prepare again"
            else:
                with self.db() as db:
                    history = [
                        Finding.model_validate(f)
                        for j in db.execute(
                            "SELECT findings FROM ops WHERE run=? ORDER BY rowid", (rid,)
                        )
                        for f in json.loads(j[0])
                    ]
                items = payload["proposals"] if action == "batch" else [payload]
                for index, item in enumerate(items):
                    if stopped.is_set() or time.time() >= deadline:
                        findings.extend(
                            Finding(
                                outcome="inconclusive",
                                reason="Unprocessed at cancellation or deadline",
                                next_action="Inspect completed evidence before starting another run.",
                            )
                            for _ in items[index:]
                        )
                        stopped.set()
                        save()
                        break
                    try:
                        if action == "batch":
                            finding = self._proposal(
                                frozen, result, item, history + findings, observe, save
                            )
                        else:
                            finding = self._draft(frozen, result, item, history, observe, save)
                    except (ValueError, SyntaxError, ValidationError) as exc:
                        finding = Finding(
                            outcome="invalid",
                            reason=str(exc)[:1200],
                            next_action="Revise this item and submit with a fresh key.",
                        )
                    findings.append(finding)
                    save()
                state = "complete"
            if expected and state in {"ready", "complete"} and snapshot(self.root) != expected:
                state, reason = "stale", "Workspace changed during execution; prepare again"
        except Exception as exc:  # noqa: BLE001 - persist unexpected worker failures for reconnects
            state = "baseline_failed" if action == "prepare" else "inconclusive"
            reason = str(exc)[:1500]
        finally:
            done.set()
            if stopped.is_set():
                state, reason = (
                    ("exhausted", "Wall-clock deadline reached")
                    if time.time() >= deadline
                    else ("cancelled", "Cancellation requested")
                )
            with self.db() as db:
                db.execute(
                    "UPDATE ops SET state=?,reason=? WHERE id=? AND state='pending'",
                    (state, reason, op),
                )

    def _proposal(self, frozen, result, item, history, observe, save) -> Finding:
        proposal = Proposal.model_validate(item)
        target = next((t for t in result.targets if t.target_id == proposal.target_id), None)
        if target is None:
            raise ValueError("Unknown target for this baseline; request its target IDs")
        data, patch = edit((frozen / target.file).read_bytes(), target, proposal)
        mid = stable([result.baseline_fingerprint, target.file, digest(data)])
        prior = next((f for f in reversed(history) if f.mutant_id == mid and f.diff), None)
        if prior and not (prior.outcome == "inconclusive" and proposal.retry_inconclusive):
            return Finding(
                outcome="duplicate",
                reason="This edit already has a result",
                next_action="Retrieve prior evidence by mutant ID.",
                mutant_id=mid,
                proposal=proposal,
            )
        if result.remaining_candidates <= 0:
            return self._exhausted()
        result.remaining_candidates -= 1
        save()
        return self._mutate(frozen, target, data, patch, mid, proposal, result.baseline, observe)

    def _draft(self, frozen, result, item, history, observe, save) -> Finding:
        prior = next(
            (
                f
                for f in reversed(history)
                if f.mutant_id == item["mutant_id"] and f.diff and f.proposal
            ),
            None,
        )
        if prior is None:
            raise ValueError("Unknown mutant_id for this run")
        if result.remaining_candidates <= 0:
            return self._exhausted()
        target = next(t for t in result.targets if t.target_id == prior.proposal.target_id)
        draft = TestPatch.model_validate(item["patch"])
        self._validate_draft(frozen, draft, item["test_ids"], item["assertion_line"])
        result.remaining_candidates -= 1
        save()
        return self._verify(frozen, target, prior, draft, item, observe)

    @staticmethod
    def _exhausted() -> Finding:
        return Finding(
            outcome="exhausted",
            reason="Candidate budget spent",
            next_action="Report remaining uncertainty.",
        )

    @staticmethod
    def _mutate(frozen, target, data, patch, mid, proposal, baseline, observe) -> Finding:
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "repo"
            shutil.copytree(frozen, copy)
            (copy / target.file).write_bytes(data)
            first = observe(copy, baseline.scope)
            observations = [first]
            verdict = classify(baseline, first)
            reason = (
                first.reason
                or {
                    "killed": "Baseline-passing test failed in its body",
                    "survived": "Same scoped tests completed with unchanged outcomes",
                    "inconclusive": "Collection, phase or outcomes differ unexpectedly",
                }[verdict]
            )
            if verdict == "killed":
                repeat = observe(copy, baseline.scope)
                observations.append(repeat)
                if classify(baseline, repeat) != "killed" or failures(first) != failures(repeat):
                    verdict, reason = (
                        "inconclusive",
                        "Failure did not reproduce at the same location",
                    )
        return Finding(
            outcome=verdict,
            reason=reason,
            next_action="Inspect the requirement; verify a focused test for a meaningful survivor.",
            mutant_id=mid,
            proposal=proposal,
            diff=patch,
            observations=observations,
        )

    @staticmethod
    def _validate_draft(frozen: Path, draft: TestPatch, ids: list[str], line: int):
        path = Path(relative(draft.path))
        if (
            path.parts[0] != "tests"
            or not path.match("test_*.py")
            or any(i.split("::")[0] != draft.path or "::" not in i for i in ids)
            or len(set(ids)) != len(ids)
        ):
            raise ValueError("Select exact candidate node IDs in one tests/**/test_*.py module")
        file = frozen / path
        if (digest(file.read_bytes()) if file.exists() else None) != draft.before_sha256:
            raise ValueError(
                "Existing test content differs; supply its frozen SHA-256 (null for a new file)"
            )
        if line not in {
            n.lineno for n in ast.walk(ast.parse(draft.content)) if isinstance(n, ast.Assert)
        }:
            raise ValueError(
                "assertion_line must point to an assert statement in the candidate module"
            )

    @staticmethod
    def _verify(frozen, target, prior, draft, item, observe) -> Finding:
        ids, line = item["test_ids"], item["assertion_line"]
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "repo"
            shutil.copytree(frozen, copy)
            file = copy / draft.path
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(draft.content.encode())
            base = observe(copy, ids)
            source = copy / target.file
            data, _ = edit(source.read_bytes(), target, prior.proposal)
            source.write_bytes(data)
            mutant = observe(copy, ids)
            observations = [base, mutant]

            def hit(obs):
                return (
                    classify(base, obs) == "killed"
                    and any(
                        c.assertion
                        and c.phase == "call"
                        and c.outcome == "failed"
                        and c.node_id in ids
                        and (c.path, c.line) == (draft.path, line)
                        for c in obs.cases
                    )
                    and all((p, l) == (draft.path, line) for _, p, l in failures(obs))
                )

            verdict: Outcome = "inconclusive"
            reason = "Candidate must pass all requested IDs and fail at the intended assertion only"
            if (
                base.green
                and sorted(base.collected) == sorted(ids)
                and all(c.outcome == "passed" for c in base.cases)
            ):
                if classify(base, mutant) == "survived":
                    verdict, reason = "undistinguished", "Candidate passed on both versions"
                elif hit(mutant):
                    repeat = observe(copy, ids)
                    observations.append(repeat)
                    if hit(repeat) and failures(mutant) == failures(repeat):
                        verdict, reason = (
                            "verified",
                            "Candidate passed on feature and reproduced the intended assertion failure",
                        )
        return Finding(
            outcome=verdict,
            reason=reason,
            next_action="Adopt the exact verified content and run the normal suite."
            if verdict == "verified"
            else "Inspect the observations and revise the candidate.",
            mutant_id=prior.mutant_id,
            patch_sha256=digest(draft.content.encode()),
            observations=observations,
        )
