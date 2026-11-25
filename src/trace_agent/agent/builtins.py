"""Tools implemented by the orchestrator itself, because they act on *task state*, not the machine.

* ``finish_task``       - end the task with a structured, checked report.
* ``record_hypothesis`` - make hypothesis formation and revision explicit and inspectable.

They are offered to the model alongside the registry tools, but never go through the
ToolExecutor: they have no side effects outside the task, so they need no authorization.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from trace_agent.agent.state import HypothesisStatus, StopReason
from trace_agent.core.messages import ToolDefinition, compact_json_schema

FINISH_TOOL_NAME = "finish_task"
HYPOTHESIS_TOOL_NAME = "record_hypothesis"
BUILTIN_TOOL_NAMES = frozenset({FINISH_TOOL_NAME, HYPOTHESIS_TOOL_NAME})


class Outcome(StrEnum):
    FIXED = "fixed"
    DIAGNOSED = "diagnosed"
    NO_FIX_NEEDED = "no_fix_needed"
    INSUFFICIENT_INFORMATION = "insufficient_information"


OUTCOME_REASON = {
    Outcome.FIXED: StopReason.VERIFIED_FIX,
    Outcome.DIAGNOSED: StopReason.DIAGNOSED,
    Outcome.NO_FIX_NEEDED: StopReason.NO_FIX_NEEDED,
    Outcome.INSUFFICIENT_INFORMATION: StopReason.INSUFFICIENT_INFORMATION,
}


class FinishInput(BaseModel):
    model_config = ConfigDict(extra="ignore")  # tolerate extra keys from chatty models

    outcome: Outcome
    summary: str = Field(min_length=1, description="One-paragraph explanation for the user.")
    root_cause: str | None = Field(default=None, description="The identified root cause, if any.")
    evidence: list[str] = Field(default_factory=list, description="Observed facts, e.g. 'src/a.py:42 uses x before init'.")
    recommended_fix: str | None = Field(default=None, description="Concrete change that would fix the problem.")
    confidence: str = Field(default="medium", pattern="^(low|medium|high)$")


class HypothesisInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    statement: str | None = Field(default=None, description="The hypothesis. Required when creating a new one.")
    status: HypothesisStatus = Field(default=HypothesisStatus.PROPOSED, description="proposed, supported or rejected.")
    evidence: list[str] = Field(default_factory=list, description="Observations for/against it, with file:line.")
    id: int | None = Field(default=None, ge=1, description="Id of an existing hypothesis to update.")


FINISH_TOOL = ToolDefinition(
    name=FINISH_TOOL_NAME,
    description=(
        "End the task and report the result to the user. Call it alone (not together with other tools). "
        "outcome must be one of: fixed, diagnosed, no_fix_needed, insufficient_information."
    ),
    parameters=compact_json_schema(FinishInput.model_json_schema()),
)

HYPOTHESIS_TOOL = ToolDefinition(
    name=HYPOTHESIS_TOOL_NAME,
    description=(
        "Record a root-cause hypothesis, or update one by id when evidence supports or rejects it. "
        "Keeps your reasoning explicit: propose, then confirm or reject with file:line evidence."
    ),
    parameters=compact_json_schema(HypothesisInput.model_json_schema()),
)

BUILTIN_TOOLS = (HYPOTHESIS_TOOL, FINISH_TOOL)
