"""Orchestration, task state and the Observe-Reason-Act-Verify loop.

Phase 0 provides task/execution state (``agent.state``). The orchestrator and
loop are Phase 2.
"""

from trace_agent.agent.state import (
    ActionRecord,
    ActionStage,
    AgentTask,
    StopReason,
    TaskStateError,
    TaskStatus,
    VerificationResult,
)

__all__ = [
    "ActionRecord",
    "ActionStage",
    "AgentTask",
    "StopReason",
    "TaskStateError",
    "TaskStatus",
    "VerificationResult",
]
