"""AgentNet Task Protocol 常量:端点路径、Header、SSE 事件类型。"""

PROTOCOL_VERSION = "0.1"

# ---------------------------------------------------------------------------
# Agent 侧端点(注册的 agent 必须实现,SDK 自动兜底)
# ---------------------------------------------------------------------------

CARD_PATH = "/card"
HEALTH_PATH = "/health"
TASKS_PATH = "/tasks"


def task_path(task_id: str) -> str:
    return f"{TASKS_PATH}/{task_id}"


def task_events_path(task_id: str) -> str:
    return f"{TASKS_PATH}/{task_id}/events"


def task_messages_path(task_id: str) -> str:
    return f"{TASKS_PATH}/{task_id}/messages"


def task_cancel_path(task_id: str) -> str:
    return f"{TASKS_PATH}/{task_id}/cancel"


AGENT_ENDPOINTS = (
    CARD_PATH,
    HEALTH_PATH,
    TASKS_PATH,
)

# ---------------------------------------------------------------------------
# Registry 侧端点前缀
# ---------------------------------------------------------------------------

API_PREFIX = "/v1"

# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------

AUTHORIZATION_HEADER = "Authorization"
BEARER_PREFIX = "Bearer "
REQUEST_ID_HEADER = "X-Request-Id"

# ---------------------------------------------------------------------------
# SSE 事件类型(GET /tasks/{id}/events 推送)
# ---------------------------------------------------------------------------

SSE_STATE = "state"  # data: {"state": "<TaskState>"}
SSE_DELTA = "delta"  # data: {"text": "<增量文本>"}
SSE_MESSAGE = "message"  # data: Message JSON(完整新消息,如澄清问题)
SSE_ARTIFACT = "artifact"  # data: Artifact JSON
SSE_ERROR = "error"  # data: {"error": "<错误信息>"}
