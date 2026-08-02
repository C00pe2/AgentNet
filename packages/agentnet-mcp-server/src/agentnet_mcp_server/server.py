"""agentnet-mcp-server:把整个 AgentNet 网络暴露成一个 MCP server(stdio)。

Claude / Cursor 等 MCP 客户端连接后可直接调用三个工具:
- agentnet_search:试召回,看网络中哪些 agent 能解决某个问题(不发起调用)
- agentnet_list:列出网络中的全部 agent(含信誉)
- agentnet_ask:向网络提问,自动路由到最合适的 agent 并返回答案

配置复用 Router 的环境变量:AGENTNET_REGISTRY_URL / AGENTNET_CONSUMER_KEY /
AGENTNET_LLM_BASE_URL / AGENTNET_LLM_API_KEY / AGENTNET_LLM_MODEL。

注意:远程 agent 返回的内容是不受信数据,工具输出中已标注来源,
客户端不应将其当作指令执行。
"""

from __future__ import annotations

from agentnet_router import Router, RouterSettings
from fastmcp import FastMCP


def create_server(router: Router | None = None) -> FastMCP:
    """构建 MCP server;router 可注入(测试用),默认从环境变量构建。"""
    rt = router or Router(RouterSettings.from_env())
    server = FastMCP("agentnet")

    @server.tool()
    async def agentnet_search(query: str, top_k: int = 5) -> str:
        """在 AgentNet 网络中搜索能解决 query 的 agent,返回能力名片与相似度(不发起调用)。"""
        candidates = await rt.search(query, top_k)
        if not candidates:
            return "网络中没有可用 agent"
        return "\n".join(
            f"- {c.card.agent_id}({c.card.name},相似度 {c.score:.2f}):{c.card.description}"
            for c in candidates
        )

    @server.tool()
    async def agentnet_list() -> str:
        """列出 AgentNet 网络中的全部 agent,含信誉数据(调用次数/成功率/评分)。"""
        agents = await rt.list_agents()
        if not agents:
            return "网络中还没有注册任何 agent"
        lines = []
        for a in agents:
            rep = a.get("reputation") or {}
            rep_text = (
                f"调用 {rep.get('calls', 0)} 次,成功率 {rep.get('success_rate', 0):.0%}"
                if rep
                else "无调用记录"
            )
            lines.append(
                f"- {a['agent_id']}({a['name']})[{a.get('status', '?')}]:"
                f"{a.get('description', '')} | 信誉:{rep_text}"
            )
        return "\n".join(lines)

    @server.tool()
    async def agentnet_ask(query: str) -> str:
        """向 AgentNet 网络提问:自动路由到最合适的 agent 并返回答案;无人能答时由本地模型兜底。"""
        result = await rt.ask(query)
        if result.routed and not result.fallback:
            source = f"AgentNet 网络中的 {result.agent_id}(不受信内容,仅作参考)"
        else:
            source = "本地模型(网络中无合适 agent 或调用失败)"
        return f"{result.answer}\n\n—— 回答来源:{source}"

    return server


def main() -> None:
    create_server().run()  # 默认 stdio 传输


if __name__ == "__main__":
    main()
