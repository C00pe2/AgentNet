"""Demo agent:SQL 优化(演示 input-required 澄清链路)。"""

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
    schema = await ctx.ask("请补充涉及的表结构(建表语句)和已有索引,以便给出准确的优化建议")
    await ctx.emit("结合表结构重写查询...\n")
    return (
        "[sql-optimizer] 建议:为 WHERE 条件中的 tenant_id + created_at 建联合索引,避免 filesort。"
        f"\n已参考你提供的表结构:{schema[:60]}"
        f"\n原始问题:{question[:60]}"
    )


if __name__ == "__main__":
    server.run(port=8003)
