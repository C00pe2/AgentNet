"""协议枚举定义。"""

from enum import StrEnum


class TaskState(StrEnum):
    """Task 状态机。

    状态流转:
        submitted -> working -> completed
                     working -> input-required -> working(收到补充消息)
                     working -> failed
                     submitted/working/input-required -> canceled
    """

    SUBMITTED = "submitted"
    WORKING = "working"
    INPUT_REQUIRED = "input-required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_STATES


_TERMINAL_STATES = frozenset({TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELED})


class MessageRole(StrEnum):
    USER = "user"
    AGENT = "agent"


class AgentStatus(StrEnum):
    """Registry 中 agent 的可用状态(巡检维护)。"""

    ACTIVE = "active"
    OFFLINE = "offline"
