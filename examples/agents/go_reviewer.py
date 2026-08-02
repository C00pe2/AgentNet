"""Demo agent:Go 代码审查(预制回答,演示协议全生命周期)。"""

import asyncio

from agentnet_core import AgentCard
from agentnet_sdk import AgentServer

server = AgentServer(
    AgentCard(
        agent_id="go-reviewer",
        name="Go Reviewer",
        description="专注 Go 代码审查,擅长并发 bug 与性能问题",
        natural_capabilities="我擅长审查 Go 代码:goroutine 泄漏、channel 死锁、竞态条件、pprof 性能分析、内存逃逸",
        capabilities=["go", "code-review", "concurrency"],
    )
)


@server.skill
async def handle(ctx):
    question = ctx.message.text_content()
    await ctx.emit("正在分析 Go 代码...\n")
    await asyncio.sleep(0.2)
    await ctx.emit("检查 goroutine 生命周期与 channel 使用...\n")
    await asyncio.sleep(0.2)
    return (
        "[go-reviewer] 审查完成:发现 1 处潜在 goroutine 泄漏(worker 退出时未关闭结果 channel)。"
        f"\n原始问题:{question[:60]}"
    )


if __name__ == "__main__":
    server.run(port=8001)
