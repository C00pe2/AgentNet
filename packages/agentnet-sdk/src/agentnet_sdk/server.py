"""AgentServer:把你的 agent 逻辑包装成 AgentNet Task Protocol 服务。

用法::

    from agentnet_core import AgentCard
    from agentnet_sdk import AgentServer

    server = AgentServer(AgentCard(agent_id="go-reviewer", name="Go Reviewer", ...))

    @server.skill
    async def handle(ctx):
        await ctx.emit("分析中...")            # 增量文本(SSE delta)
        answer = await ctx.ask("哪个分支?")    # 中途澄清(input-required)
        await ctx.add_artifact("fix.diff", [FilePart(...)])
        return "最终结论"                      # 完成

    server.run(port=8001)

provider 只写业务逻辑;状态机、SSE、/card、鉴权全部由 AgentServer 兜底。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime, timezone

import uvicorn
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse

from agentnet_core import (
    AgentCard,
    Artifact,
    Message,
    MessageAppend,
    Part,
    Task,
    TaskCreate,
    constants,
)
from agentnet_core.enums import TaskState
from agentnet_core.sse import format_event


class TaskContext:
    """handler 的交互接口。"""

    def __init__(self, runtime: _TaskRuntime) -> None:
        self._rt = runtime

    @property
    def task(self) -> Task:
        return self._rt.task

    @property
    def message(self) -> Message:
        """最近一条用户消息。"""
        for msg in reversed(self._rt.task.messages):
            if msg.role == "user":
                return msg
        raise RuntimeError("task 中没有用户消息")

    async def emit(self, text: str) -> None:
        """推送增量文本(SSE delta);完成时会聚合成最终 agent 消息。"""
        self._rt.delta_buffer.append(text)
        self._rt.broadcast(constants.SSE_DELTA, {"text": text})

    async def ask(self, question: str) -> str:
        """向用户提问并等待回答(input-required);返回用户答案文本。"""
        rt = self._rt
        msg = Message.agent_text(question)
        rt.task.messages.append(msg)
        rt.broadcast(constants.SSE_MESSAGE, json.loads(msg.model_dump_json()))
        rt.set_state(TaskState.INPUT_REQUIRED)
        rt.waiter = asyncio.get_running_loop().create_future()
        answer: str = await rt.waiter  # 由 POST /messages 唤醒;cancel 时抛 CancelledError
        rt.set_state(TaskState.WORKING)
        return answer

    async def add_artifact(self, name: str, parts: list[Part]) -> None:
        artifact = Artifact(name=name, parts=parts)
        self._rt.task.artifacts.append(artifact)
        self._rt.broadcast(constants.SSE_ARTIFACT, json.loads(artifact.model_dump_json()))


class _TaskRuntime:
    def __init__(self, task: Task) -> None:
        self.task = task
        self.subscribers: set[asyncio.Queue] = set()
        self.runner: asyncio.Task | None = None
        self.waiter: asyncio.Future | None = None
        self.delta_buffer: list[str] = []

    def set_state(self, state: TaskState) -> None:
        self.task.state = state
        self.task.updated_at = datetime.now(timezone.utc)
        self.broadcast(constants.SSE_STATE, {"state": state.value})

    def broadcast(self, event: str, data: dict) -> None:
        payload = format_event(event, json.dumps(data, ensure_ascii=False))
        for q in list(self.subscribers):
            q.put_nowait(payload)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        # 迟到订阅者:补发当前状态;若已终结直接收尾
        q.put_nowait(format_event(constants.SSE_STATE, json.dumps({"state": self.task.state.value})))
        if self.task.is_terminal:
            q.put_nowait(None)
        self.subscribers.add(q)
        return q

    def close_subscribers(self) -> None:
        for q in list(self.subscribers):
            q.put_nowait(None)


Handler = Callable[[TaskContext], Awaitable[str | None] | AsyncIterator[str] | str | None]


class AgentServer:
    def __init__(self, card: AgentCard) -> None:
        self.card = card
        self._handler: Handler | None = None
        self._tasks: dict[str, _TaskRuntime] = {}
        self.app = FastAPI(title=card.name, version=card.version)
        self._mount()

    def skill(self, fn: Handler) -> Handler:
        """注册任务处理函数。"""
        self._handler = fn
        return fn

    def run(self, host: str = "0.0.0.0", port: int = 8000) -> None:
        uvicorn.run(self.app, host=host, port=port)

    # ------------------------------------------------------------------
    # 内部:鉴权
    # ------------------------------------------------------------------

    def _check_auth(self, authorization: str | None) -> None:
        if self.card.auth.type == "bearer" and self.card.auth.token:
            expect = f"Bearer {self.card.auth.token}"
            if authorization != expect:
                raise HTTPException(401, "token 无效")

    # ------------------------------------------------------------------
    # 内部:任务执行
    # ------------------------------------------------------------------

    async def _run(self, rt: _TaskRuntime) -> None:
        rt.set_state(TaskState.WORKING)
        ctx = TaskContext(rt)
        try:
            produced = self._handler(ctx)  # type: ignore[misc]
            if inspect.isasyncgen(produced):
                async for chunk in produced:
                    if isinstance(chunk, str):
                        await ctx.emit(chunk)
                produced = None
            elif inspect.isawaitable(produced):
                produced = await produced
            if isinstance(produced, str) and produced:
                rt.task.messages.append(Message.agent_text(produced))
            elif rt.delta_buffer:
                rt.task.messages.append(Message.agent_text("".join(rt.delta_buffer)))
            rt.set_state(TaskState.COMPLETED)
        except asyncio.CancelledError:
            rt.set_state(TaskState.CANCELED)
            raise
        except Exception as exc:  # noqa: BLE001 - agent 业务异常收敛为 failed
            rt.task.error = f"{exc.__class__.__name__}: {exc}"
            rt.broadcast(constants.SSE_ERROR, {"error": rt.task.error})
            rt.set_state(TaskState.FAILED)
        finally:
            rt.close_subscribers()

    # ------------------------------------------------------------------
    # 内部:协议端点
    # ------------------------------------------------------------------

    def _get_runtime(self, task_id: str) -> _TaskRuntime:
        rt = self._tasks.get(task_id)
        if rt is None:
            raise HTTPException(404, f"task {task_id!r} 不存在")
        return rt

    def _mount(self) -> None:
        app = self.app

        @app.get(constants.CARD_PATH)
        async def get_card(authorization: str | None = Header(None)) -> dict:
            self._check_auth(authorization)
            return json.loads(self.card.model_dump_json())

        @app.get(constants.HEALTH_PATH)
        async def health() -> dict:
            return {"status": "ok"}

        @app.post(constants.TASKS_PATH, status_code=200)
        async def create_task(
            body: TaskCreate, authorization: str | None = Header(None)
        ) -> dict:
            self._check_auth(authorization)
            if self._handler is None:
                raise HTTPException(500, "未注册 skill handler")
            task = Task(id=uuid.uuid4().hex[:16], agent_id=self.card.agent_id)
            task.messages.append(body.message)
            rt = _TaskRuntime(task)
            self._tasks[task.id] = rt
            rt.runner = asyncio.create_task(self._run(rt))
            return json.loads(task.model_dump_json())

        @app.get(constants.TASKS_PATH + "/{task_id}")
        async def get_task(task_id: str, authorization: str | None = Header(None)) -> dict:
            self._check_auth(authorization)
            return json.loads(self._get_runtime(task_id).task.model_dump_json())

        @app.post(constants.TASKS_PATH + "/{task_id}/messages")
        async def append_message(
            task_id: str, body: MessageAppend, authorization: str | None = Header(None)
        ) -> dict:
            self._check_auth(authorization)
            rt = self._get_runtime(task_id)
            rt.task.messages.append(body.message)
            if rt.task.state == TaskState.INPUT_REQUIRED and rt.waiter is not None and not rt.waiter.done():
                rt.waiter.set_result(body.message.text_content())
                return json.loads(rt.task.model_dump_json())
            raise HTTPException(400, "task 不在等待输入状态")

        @app.post(constants.TASKS_PATH + "/{task_id}/cancel")
        async def cancel_task(task_id: str, authorization: str | None = Header(None)) -> dict:
            self._check_auth(authorization)
            rt = self._get_runtime(task_id)
            if not rt.task.is_terminal and rt.runner is not None:
                rt.runner.cancel()
            return json.loads(rt.task.model_dump_json())

        @app.get(constants.TASKS_PATH + "/{task_id}/events")
        async def task_events(task_id: str, authorization: str | None = Header(None)):
            self._check_auth(authorization)
            rt = self._get_runtime(task_id)
            queue = rt.subscribe()

            async def stream() -> AsyncIterator[str]:
                try:
                    while True:
                        item = await queue.get()
                        if item is None:
                            break
                        yield item
                finally:
                    rt.subscribers.discard(queue)

            return StreamingResponse(stream(), media_type="text/event-stream")
