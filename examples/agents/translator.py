"""Demo agent:中英互译。"""

import asyncio

from agentnet_core import AgentCard, AgentPricing
from agentnet_sdk import AgentServer

server = AgentServer(
    AgentCard(
        agent_id="translator",
        name="Translator",
        description="中英互译,文档级翻译",
        natural_capabilities="中英文互译,技术文档翻译,markdown 格式保留,术语一致性,本地化表达",
        capabilities=["translate", "zh-en"],
        pricing=AgentPricing(model="per-call", price=1.0),
    )
)


@server.skill
async def handle(ctx):
    question = ctx.message.text_content()
    await ctx.emit("翻译中...\n")
    await asyncio.sleep(0.2)
    return f"[translator] 翻译完成(示例输出,未接真实模型)。\n原始内容:{question[:60]}"


if __name__ == "__main__":
    server.run(port=8002)
