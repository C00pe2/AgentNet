"""Demo agent:SQL 优化。"""

import asyncio

from agentnet_core import AgentCard
from agentnet_sdk import AgentServer

server = AgentServer(
    AgentCard(
        agent_id="sql-optimizer",
        name="SQL Optimizer",
        description="SQL 查询优化与索引建议",
        natural_capabilities="SQL 查询优化,索引设计,执行计划(explain)分析,慢查询排查,JOIN/子查询重写",
        capabilities=["sql", "mysql", "performance"],
    )
)


@server.skill
async def handle(ctx):
    question = ctx.message.text_content()
    await ctx.emit("分析执行计划...\n")
    await asyncio.sleep(0.2)
    return (
        "[sql-optimizer] 建议:为 WHERE 条件中的 tenant_id + created_at 建联合索引,避免 filesort。"
        f"\n原始问题:{question[:60]}"
    )


if __name__ == "__main__":
    server.run(port=8003)
