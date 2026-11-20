from typing import Literal

from pydantic import BaseModel, ConfigDict


class Finding(BaseModel):
    """A problem found in a build, routed back to the builder that owns it. `fixable` is False
    when that builder cannot act on it (the part is computed in code, or the checker failed)."""

    model_config = ConfigDict(extra="forbid")

    check: str
    owner: Literal["data", "ui"]
    visual_id: str | None
    message: str
    evidence: dict = {}
    fixable: bool = True


class CheckResult(BaseModel):
    """What one check found: passed is true exactly when there are no findings."""

    model_config = ConfigDict(extra="forbid")

    name: str
    passed: bool
    findings: list[Finding]
    evidence: dict = {}
