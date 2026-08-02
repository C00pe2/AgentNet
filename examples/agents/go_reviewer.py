"""Demo agent:Go 代码审查(预制回答,演示协议全生命周期 + artifact 产出)。"""

import asyncio

from agentnet_core import AgentCard, TextPart
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
    diff = (
        "--- a/worker.go\n"
        "+++ b/worker.go\n"
        "@@ func worker() {\n"
        "-\tgo func() { results <- work() }()\n"
        "+\tgo func() { defer close(results); results <- work() }()"
    )
    await ctx.add_artifact("fix.diff", [TextPart(text=diff)])
    return (
        "[go-reviewer] 审查完成:发现 1 处潜在 goroutine 泄漏(worker 退出时未关闭结果 channel),"
        "修复方案见附件 fix.diff。"
        f"\n原始问题:{question[:60]}"
    )


if __name__ == "__main__":
    server.run(port=8001)
