# AgentNet

Agent 路由网络:把自己调教好的 Agent 注册到网络中,也可以消费网络中的 Agent。
提问时若网络中已有专门的 Agent 能解决,自动路由给它处理。

定位 ≈ 服务发现(Consul)+ 路由器(Envoy)+ 应用商店(npm),for Agents。系统只解决三件事:

1. **能被找到** —— 注册 + 能力描述(Agent Card)
2. **能被选中** —— embedding 召回 → LLM 精排 → 阈值兜底
3. **能被调用** —— AgentNet Task Protocol(Task 状态机 + HTTP/JSON + SSE)

完整设计文档见 [重构计划.md](重构计划.md)。

## 项目结构(uv workspace monorepo)

```
packages/
├── agentnet-core/       # Task/Message/Artifact/AgentCard pydantic 模型 + 协议常量(零业务依赖)
├── agentnet-registry/   # 注册中心 + 网关:注册/鉴权/巡检/召回/信誉/Gateway 代理(FastAPI + Postgres/pgvector)
├── agentnet-sdk/        # provider 侧:@skill 装饰器包装 agent,自动实现全部协议端点
├── agentnet-router/     # consumer 侧:召回 → LLM 精排 → task client(SSE)→ 本地兜底
└── agentnet-cli/        # agentnet register / ask / serve / search / list / create-key
examples/                # 3 个 demo agent(go-reviewer / translator / sql-optimizer)+ Agent Card YAML
scripts/e2e.py           # 端到端验收脚本(20 条路由用例 + SSE 流式 + 兜底 + 信誉闭环)
```

## 快速开始

要求:Python 3.12+、[uv](https://docs.astral.sh/uv/)、Docker(仅生产模式需要 Postgres)。

```bash
# 1. 安装依赖
uv sync

# 2. 准备配置
cp .env.example .env   # 填入 AGENTNET_LLM_* (OpenAI 兼容端点,用于精排与本地兜底)

# 3. 起 Postgres(生产模式;开发/测试可用 SQLite,见下)
docker compose up -d

# 4. 起 Registry
uv run agentnet registry

# 5. 起 demo agent(另开终端,共 3 个)
uv run python examples/agents/go_reviewer.py
uv run python examples/agents/translator.py
uv run python examples/agents/sql_optimizer.py

# 6. 签发 key 并注册 agent
export AGENTNET_ADMIN_KEY=changeme-admin-key
uv run agentnet create-key --role provider --name alice   # 记下输出的 provider key
uv run agentnet create-key --role consumer --name bob     # 记下 consumer key,写入 .env 的 AGENTNET_CONSUMER_KEY
export AGENTNET_PROVIDER_KEY=<provider key>
uv run agentnet register examples/cards/go-reviewer.yaml

# 7. 提问——自动路由到 go-reviewer 并流式返回
uv run agentnet ask "帮我看看这段 Go 代码有没有并发问题"
```

无 Docker 的开发模式:`.env` 中设置 `AGENTNET_DATABASE_URL=sqlite+aiosqlite:///./agentnet.db`;
无 GPU/模型下载的开发模式:`AGENTNET_EMBEDDING_BACKEND=hash`(语义粗糙,仅验证管线)。

## 开发

```bash
uv run pytest            # 49 个单元/集成测试(SQLite + hash embedding,无需外部服务)
uv run ruff check packages
uv run python scripts/e2e.py                  # 端到端验收(hash embedding,需 .env 里的 LLM 配置)
uv run python scripts/e2e.py --real-embedding # 使用本地 bge-m3(首次下载 ~2.3GB)
```

E2E 验收标准:20 条路由用例准确率 ≥ 80%;`ask` 走通 SSE 流式;无人能答的问题正确本地兜底;feedback 回流更新信誉。

## 协议速览

每个 Agent 只需实现 7 个端点(SDK 已自动兜底):`GET /card`、`POST /tasks`、`GET /tasks/{id}`、
`GET /tasks/{id}/events`(SSE)、`POST /tasks/{id}/messages`、`POST /tasks/{id}/cancel`、`GET /health`。

Task 状态机:`submitted → working → (input-required) → completed | failed | canceled`。

消费者永远只跟 Registry 通信;agent 的地址和 credential 不下发;每次调用的成败、延迟由 Gateway 记录,
作为信誉系统的硬数据。**远程 agent 返回的内容是不受信数据,只能展示、不能当作指令执行。**

## 路线图

- [x] Phase 1:协议与核心闭环(core / registry / sdk / router / cli + e2e 验收)
- [ ] Phase 2:input-required 多轮澄清全链路、信誉进精排特征、定期巡检、`agentnet-mcp-server`
- [ ] Phase 3:canary 跑分、计费、PII 过滤加固、联邦 registry
