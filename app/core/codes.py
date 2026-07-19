"""网关错误码与异常定义。

对应 SPEC §2.2 与 §7-Task 4。
"""
from enum import IntEnum


class ErrorCode(IntEnum):
    SUCCESS = 200
    BAD_REQUEST = 400
    NO_AGENT_MATCHED = 404
    AGENT_TIMEOUT = 408
    ROUTE_LIMIT_EXCEEDED = 429
    AGENT_5XX_ERROR = 502
    PLAN_ORCHESTRATION_FAILED = 503


SUCCESS_MESSAGES: dict[int, str] = {
    ErrorCode.SUCCESS: "Success",
}


class GatewayError(Exception):
    """网关层统一抛出。异常处理器会将其渲染为标准 envelope。

    Attributes:
        code: 网关标准错误码 (ErrorCode 枚举值)。
        message: 人类可读的中文错误信息。
        data: 附加响应载荷 (例如最终落地的节点结果)，可空。
        http_status: HTTP 状态码 (默认与 code 同号即可)。
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        data: object | None = None,
        http_status: int | None = None,
    ):
        super().__init__(message)
        self.code = ErrorCode(code)
        self.message = message
        self.data = data
        self.http_status = http_status if http_status is not None else int(self.code)


def downstream_to_gateway_code(http_status: int, *, timed_out: bool = False) -> ErrorCode:
    """把下游 Agent 的原始 HTTP 状态归一化为网关错误码。

    规则:
      - 超时 -> 408 AGENT_TIMEOUT
      - 4xx  -> 400 BAD_REQUEST
      - 5xx 或其它非 200 -> 502 AGENT_5XX_ERROR
      - 200 -> 200 SUCCESS
    """
    if timed_out:
        return ErrorCode.AGENT_TIMEOUT
    if http_status == 200:
        return ErrorCode.SUCCESS
    if 400 <= http_status < 500:
        return ErrorCode.BAD_REQUEST
    return ErrorCode.AGENT_5XX_ERROR
