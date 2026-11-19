from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

Key = Annotated[str, Field(min_length=1, max_length=128)]
Scope = Annotated[list[Key], Field(min_length=1, max_length=32)]
Outcome = Literal[
    "pending",
    "ready",
    "baseline_failed",
    "invalid",
    "duplicate",
    "killed",
    "survived",
    "inconclusive",
    "verified",
    "undistinguished",
    "stale",
    "exhausted",
    "cancelled",
    "complete",
    "busy",
]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Limits(Input):
    max_candidates: int = Field(default=8, ge=1, le=32)
    test_seconds: int = Field(default=30, ge=1, le=120)
    wall_seconds: int = Field(default=300, ge=5, le=900)


class Proposal(Input):
    target_id: Key
    expected_anchor: str = Field(min_length=1, max_length=512)
    replacement: str = Field(min_length=1, max_length=512)
    rationale: str = Field(min_length=1, max_length=512)
    distinguishing_input: str = Field("", max_length=512)
    retry_inconclusive: bool = False


class TestPatch(Input):
    path: Key
    before_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    content: str = Field(min_length=1, max_length=32768)


class Target(BaseModel):
    target_id: str
    symbol: str
    file: str
    line: int
    column: int
    expected_anchor: str
    excerpt: str
    static_callers: list[str]


class Case(BaseModel):
    node_id: str
    outcome: str
    phase: str
    path: str = ""
    line: int = 0
    assertion: bool = False
    evidence: str = ""


class Observation(BaseModel):
    scope: list[str]
    exit_code: int = -1
    collected: list[str] = []
    cases: list[Case] = []
    reason: str = ""
    seconds: float = 0

    @property
    def green(self) -> bool:
        return (
            not self.reason
            and self.exit_code == 0
            and sorted(self.collected) == sorted(c.node_id for c in self.cases)
            and any(c.outcome == "passed" for c in self.cases)
            and all(c.outcome in {"passed", "skipped", "xfailed"} for c in self.cases)
        )


class Finding(BaseModel):
    outcome: Outcome
    reason: str
    next_action: str
    mutant_id: str | None = None
    proposal: Proposal | None = None
    diff: str = ""
    patch_sha256: str = ""
    observations: list[Observation] = []


class Result(BaseModel):
    run_id: str
    operation_id: str
    state: Outcome
    reason: str = ""
    next_action: str = ""
    baseline_fingerprint: str = ""
    runner_image: str = ""
    requirement: str = ""
    scope: list[str] = []
    limits: Limits = Limits()
    remaining_candidates: int = 0
    remaining_seconds: float = 0
    baseline: Observation | None = None
    targets: list[Target] = []
    diff_excerpt: str = ""
    findings: list[Finding] = []
    next_offset: int | None = None
    pending_operations: list[str] = []
