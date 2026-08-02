"""SSE 线路格式助手:SDK 服务端格式化、Registry 代理/Router 客户端解析共用。"""

from __future__ import annotations


def format_event(event: str, data: str) -> str:
    """序列化一个 SSE 事件块。data 必须是单行(JSON 天然满足)。"""
    return f"event: {event}\ndata: {data}\n\n"


class SseParser:
    """逐行解析 SSE 流;一个事件块(空行分隔)结束时返回 (event, data),否则返回 None。"""

    def __init__(self) -> None:
        self._event: str | None = None
        self._data: list[str] = []

    def feed(self, line: str) -> tuple[str, str] | None:
        line = line.rstrip("\r")
        if line == "":
            if self._event is None and not self._data:
                return None
            event = self._event or "message"
            data = "\n".join(self._data)
            self._event, self._data = None, []
            return event, data
        if line.startswith(":"):
            return None  # 注释/心跳行
        if line.startswith("event:"):
            self._event = line[len("event:"):].strip()
        elif line.startswith("data:"):
            self._data.append(line[len("data:"):].lstrip())
        return None
