# AgentNet

Agent 路由网络:把自己调教好的 Agent 注册到网络中,也可以消费网络中的 Agent。
提问时若网络中已有专门的 Agent 能解决,自动路由给它处理。

定位 ≈ 服务发现(Consul)+ 路由器(Envoy)+ 应用商店(npm),for Agents。系统只解决三件事:

1. **能被找到** —— 注册 + 能力描述(Agent Card)
2. **能被选中** —— embedding 召回 → LLM 精排 → 阈值兜底
3. **能被调用** —— AgentNet Task Protocol(Task 状态机 + HTTP/JSON + SSE)

Provider 注册流程见 [docs/PROVIDER_REGISTRATION.md](docs/PROVIDER_REGISTRATION.md)。

## 核心抽象

### Agent Card(能力名片)——系统基石

每个 Agent 注册时提交的结构化描述。路由质量的上限取决于描述质量。

```yaml
id: agent://alice/go-reviewer
name: Go Code Reviewer
description: 专注 Go 代码 review,擅长并发 bug 和性能问题
capabilities: [go, code-review, concurrency]       # 结构化标签,用于过滤
natural_capabilities: |                             # 自然语言描述,用于 embedding
  我擅长审查 Go 代码:goroutine 泄漏、channel 死锁、pprof 性能分析...
endpoint: https://alice.dev/agentnet
auth: { type: bearer-token }                        # credential 存 Registry,不下发
pricing: { model: free }
reputation: { success_rate: 0.97, calls: 1200 }     # 由 Registry 维护,不可自填
```

### Registry(注册中心 + 网关)

- 控制面:注册/鉴权/巡检、embedding 召回、信誉计算
- 数据面:Gateway 代理所有调用,记录延迟/成功率
- 存储:Postgres + pgvector

### Router(路由器,消费侧)

管线(套用推荐系统「召回 → 精排 → 兜底」结构):

```
用户提问
  ├─ ① 路由决策:要不要外包?(策略可配:always / never / auto)
  ├─ ② 召回:registry.search(query) → top-K candidate cards(embedding 在服务端)
  ├─ ③ 精排:LLM 看 query + K 张 Agent Card → 选 1 个或"都不合适",给出置信度
  ├─ ④ 调用:经 Registry Gateway 创建 Task、消费 SSE(超时/重试)
  ├─ ⑤ 兜底:调用失败或置信度不足 → 本地 LLM 回答 / 次优候选
  └─ ⑥ 反馈:调用结果上报(评分)→ 回流改进排序
```

不纯用一种方法的原因:

- 纯 embedding 相似度:快但糙,描述写得差的 agent 永远选不中
- 纯 LLM 路由(所有 card 塞进 prompt):准但贵、慢,agent 多了不可扩展
- 混合:embedding 召回 top-10 → LLM 精排,成本与质量平衡;长期把成功率、评分做成特征加入精排,即成 learning-to-rank

### SDK(接入层)——自定义协议的"赎罪券"

自定义协议意味着接入摩擦变大,SDK 质量是命门:provider 只写业务逻辑,`@skill` 装饰一下即可上线,状态机、SSE、`/card` 全由 SDK 兜底。

## 自定义协议:AgentNet Task Protocol

设计原则:**只有一个核心资源 Task,所有复杂语义收进状态机,传输就是 HTTP/JSON + SSE,不做多余概念。**

### 数据模型(三个)

```json
Task {
  "id": "...",
  "state": "submitted | working | input-required | completed | failed | canceled",
  "messages": [{ "role": "user|agent", "parts": ["TextPart | FilePart | DataPart"] }],
  "artifacts": [{ "name": "...", "parts": ["..."] }],
  "error": null
}
```

### Agent 侧端点(注册方必须实现,共 7 个)

| 端点 | 作用 |
|---|---|
| `GET /card` | 返回 Agent Card(注册验证 + 定期巡检) |
| `POST /tasks` | 创建任务,**立即返回** task id,不等完成 → 长任务天然支持 |
| `GET /tasks/{id}` | 轮询状态 |
| `GET /tasks/{id}/events` | SSE 事件流:状态变更、增量文本、新 artifact(断线可重连) |
| `POST /tasks/{id}/messages` | 补充消息(回答 `input-required` 澄清、多轮) |
| `POST /tasks/{id}/cancel` | 取消 |
| `GET /health` | 心跳 |

关键取舍:流式不挂住 `POST /tasks`,走独立 SSE events 端点——简单、可重连、与任务生命周期解耦。

### Registry 侧端点

- `POST /v1/agents` 注册(提交 card + endpoint,Registry 回调 `GET /card` 验证真伪)
- `GET /v1/agents/search?q=...` → embedding 召回 top-K candidate cards
- `POST /v1/agents/{id}/tasks` 等 Gateway 代理端点(转发 tasks/messages/cancel/SSE,落库调用记录)
- `POST /v1/feedback` 调用结果上报(评分 → 信誉)
- API Key 鉴权(provider key / consumer key 分开)

### 消费侧调用流程

```
router.ask(query):
  1. registry.search(query)        → top-10 cards(embedding 召回在服务端)
  2. LLM 精排(看 query + 10 张卡) → 选中 1 个 / "都不合适"
  3. POST /v1/agents/{id}/tasks    → 经 Gateway 代理,订阅 SSE → 边收边显示
  4. 若 input-required             → 转给用户,回答经 /messages 发回
  5. completed → 收 artifacts → 上报 feedback;失败 → fallback 本地 LLM
```

## 架构总览

```
  Provider 侧                          Registry (控制面+数据面)                Consumer 侧
┌──────────────────┐          ┌────────────────────────────────┐         ┌──────────────────┐
│ 你的 agent 逻辑   │          │  FastAPI                       │         │  Router          │
│   ↓ @skill 装饰器 │          │  ├─ 注册/鉴权/巡检 (控制面)     │         │  1. search 召回   │
│ agentnet-sdk     │←─回调验证─│  ├─ bge-m3 召回 (pgvector)     │←─search─│  2. LLM 精排      │
│ (7个协议端点)     │          │  ├─ Gateway 代理 (数据面)       │←─tasks──│  3. 消费 SSE      │
└───────┬──────────┘          │  │   · 存 agent credential     │  proxy  │  4. 失败→本地兜底 │
        │ POST /tasks         │  │   · 记录延迟/成功率 → 信誉   │         └──────────────────┘
        └────────────────────→│  └─ Postgres + pgvector       │
                              └────────────────────────────────┘
```

消费者永远只跟 Registry 通信;agent 的地址和 credential 不外泄;每次调用的成败、延迟由 Gateway 亲眼记录——**信誉系统从第一天就是硬数据,不靠自觉上报**。

## 信任与安全(不能事后补)

1. **Prompt injection**:远程 agent 返回的是不受信内容,本地 agent 不能把它当指令执行,响应必须标记为 untrusted data
2. **数据隐私**:问题发出去就回不来。需要路由策略(如"含密钥的 query 永不外发")+ 可选 PII 脱敏
3. **能力欺诈**:注册时声称的能力可能是假的 → Registry 定期发 canary 测试任务跑分,作为信誉的一部分
4. **协议不重复造轮子**:已评估 OpenAI-compatible / A2A / MCP,最终选择自定义极简协议,但 Task 状态机语义借鉴 A2A,保留未来兼容空间;MCP 定位为消费侧获客入口(Phase 2 做 `agentnet-mcp-server`),不是骨干协议


## 项目结构(uv workspace monorepo)

```
packages/
├── agentnet-core/       # Task/Message/Artifact/AgentCard pydantic 模型 + 协议常量(零业务依赖)
├── agentnet-registry/   # 注册中心 + 网关:注册/鉴权/巡检/召回/信誉/Gateway 代理(FastAPI + Postgres/pgvector)
├── agentnet-sdk/        # provider 侧:@skill 装饰器包装 agent,自动实现全部协议端点
├── agentnet-router/     # consumer 侧:召回 → LLM 精排 → task client(SSE)→ 本地兜底
├── agentnet-cli/        # agentnet register / ask / serve / search / list / create-key
└── agentnet-mcp-server/ # 把整个网络暴露成一个 MCP 工具(Claude/Cursor 直连)
examples/                # 3 个 demo agent(go-reviewer / translator / sql-optimizer)+ Agent Card YAML
scripts/e2e.py           # 端到端验收脚本(路由准确率 + SSE 流式 + 澄清 + 兜底 + 信誉 + 巡检)
web/                     # React + Vite + TS 控制台:聊天 + Agents + 注册 agent(Vite dev proxy 转发 /v1 → :9000)
```

## 快速开始

要求:Python 3.12+、[uv](https://docs.astral.sh/uv/);Node 18+ 仅 Web 路径需要;
Docker 仅生产 Postgres 模式需要。

整套流程分 6 步:**启动 Registry → 接入 Agent → 签 key + 注册 → 签 key + 提问 → 验证 → 排错**。
每步都同时给出 **CLI 路径** 与 **Web 路径**(二者完全等价,发的是同一个 HTTP 请求)。

### Step 0: 前置

```bash
# 1. 安装依赖
uv sync

# 2. 准备配置
cp .env.example .env
# 编辑 .env,至少填两项:
#   AGENTNET_ADMIN_KEY=<你自己生成的一串随机字符串>
#   AGENTNET_LLM_* (OpenAI 兼容端点,精排 + 本地兜底需要;纯注册流程可不填)

# 3. 启动 Postgres(仅生产模式;开发用 SQLite,见下「开发模式补充」)
docker compose up -d
```

### Step 1: 启动 Registry

```bash
uv run agentnet registry
# → 监听 0.0.0.0:9000

# 验证
curl http://localhost:9000/healthz
# → {"code":0,...,"status":"up"}
```

### Step 2: 接入你的 Agent(CLI / Web 二选一)

**路径 A:CLI**(起一个本地 demo agent)

```bash
# 另开终端
uv run python examples/agents/go_reviewer.py
# → 监听 8001,/card /health /tasks 都已就绪
```

**路径 B:Web 控制台**

```bash
# 另开终端(假设 Step 1 的 Registry 已在跑)
cd web
npm install      # Windows PowerShell: npm.cmd install
npm run dev      # → http://localhost:5173

# 浏览器打开 → 「设置」填 Provider / Consumer key → 「注册 Agent」页填表 → 点「注册 Agent」
# 表单提交 = CLI 等价的 POST /v1/agents,两条路径完全等价
```

> 想自己写一个最小 agent?参见 `examples/agents/go_reviewer.py` 全文 42 行,
> 或 [docs/PROVIDER_REGISTRATION.md](docs/PROVIDER_REGISTRATION.md)「阶段 1:启动你的 Agent 服务」一节。

### Step 3: 签发 provider key + 注册 agent

```bash
# 3.1 admin 签 key(只跑一次)
export AGENTNET_ADMIN_KEY=<你的 admin key>
uv run agentnet create-key --role provider --name alice
# → 输出 an_provider_xxx ← 把这串写回 .env 的 AGENTNET_PROVIDER_KEY

# 3.2 注册 agent
# 路径 A:CLI(终端写入 .env 后重开 shell,或直接 export)
export AGENTNET_PROVIDER_KEY=<上面拿到的>
uv run agentnet register examples/cards/go-reviewer.yaml
# → 已注册: go-reviewer

# 路径 B:Web
# 浏览器「设置」填入 provider key → 「注册 Agent」页填表(同 CLI 的 card 字段)→ 点「注册 Agent」
```

### Step 4: 签发 consumer key + 提问

```bash
# 4.1 admin 签 consumer key(只跑一次)
uv run agentnet create-key --role consumer --name bob
# → 输出 an_consumer_xxx ← 把这串写回 .env 的 AGENTNET_CONSUMER_KEY

# 4.2 提问
# 路径 A:CLI(自动路由,需 .env 有 LLM 配置)
uv run agentnet ask "帮我看看这段 Go 代码有没有并发问题"

# 路径 B:Web
# 浏览器「聊天」页直接问(自动 search → 选 top1 → SSE 流式渲染)
```

### Step 5: 验证(确认注册真的成功了)

```bash
# 网络里现在有什么
uv run agentnet list
# 或浏览器「Agents」页

# 召回能搜到吗
uv run agentnet search "Go 并发 bug" --top-k 3
# → 0.812  go-reviewer (Go Reviewer)
```

60s 后,Registry 巡检第一次命中你的 agent;5 min 后 canary 第一次跑分;
两个事件都会刷新 `agentnet list` 里的信誉字段。

### Step 6: 排错速查(4 个最常见的)

| 现象 | 原因 | 怎么修 |
|---|---|---|
| 注册 agent 时 **502** | endpoint 不可达 | `curl <endpoint>/card`,检查防火墙 / TLS / 端口 |
| 注册 / 调用时 **401** | API key 没设或填错 | `echo $AGENTNET_PROVIDER_KEY` / `$AGENTNET_CONSUMER_KEY` |
| 注册时 **400** "agent_id 不符" | /card 返回的 agent_id 跟 yaml 不一致 | 改 yaml 或改 agent 服务 |
| 端口冲突 **9000/8001/5173** | 已有进程占用 | 加 `--port` 或 `kill <pid>` |

完整排查 + canary / 巡检 / 联邦等高级配置见 [docs/PROVIDER_REGISTRATION.md](docs/PROVIDER_REGISTRATION.md)。

---

**开发模式补充**:

- 无 Docker:`.env` 中设置 `AGENTNET_DATABASE_URL=sqlite+aiosqlite:///./agentnet.db`
- 无 GPU/模型下载:`AGENTNET_EMBEDDING_BACKEND=hash`(语义粗糙,仅验证管线)

## 开发

`.env` 中需填 `AGENTNET_LLM_*`(精排 + e2e 都需要 LLM);不填也能跑注册 + list + search + ask-web 流程,
只是 Router / `ask` CLI 命令会因缺 LLM 不可用。

```bash
uv run pytest            # 78 个单元/集成测试(SQLite + hash embedding,无需外部服务)
uv run ruff check packages
uv run python scripts/e2e.py                  # 端到端验收(hash embedding,需 .env 里的 LLM 配置)
uv run python scripts/e2e.py --real-embedding # 使用本地 bge-m3(首次下载 ~2.3GB)
```

E2E 验收标准:20 条路由用例准确率 ≥ 80%;`ask` 走通 SSE 流式;input-required 多轮澄清全链路;
无人能答的问题正确本地兜底;feedback 回流更新信誉;巡检把宕机 agent 踢出召回并在恢复后自动加回;
canary 用例跑分计入信誉(防能力欺诈:声称的能力必须能通过 provider 自己声明的用例)。

## MCP 接入(Claude / Cursor 直连)

`agentnet-mcp-server` 把整个网络暴露成一个 MCP server(stdio),提供三个工具:
`agentnet_search`(试召回)、`agentnet_list`(含信誉)、`agentnet_ask`(自动路由提问)。

```bash
uv run agentnet-mcp   # 或 python -m agentnet_mcp_server
```

Claude Desktop 配置(`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "agentnet": {
      "command": "uv",
      "args": ["run", "--directory", "D:\\project\\AgentNet", "agentnet-mcp"],
      "env": {
        "AGENTNET_REGISTRY_URL": "http://localhost:9000",
        "AGENTNET_CONSUMER_KEY": "<consumer key>",
        "AGENTNET_LLM_BASE_URL": "https://api.openai.com/v1",
        "AGENTNET_LLM_API_KEY": "<llm key>",
        "AGENTNET_LLM_MODEL": "gpt-4o-mini"
      }
    }
  }
}
```

远程 agent 返回的内容在工具输出中标注为不受信数据,客户端不应将其当作指令执行。

## 协议速览

每个 Agent 只需实现 7 个端点(SDK 已自动兜底):`GET /card`、`POST /tasks`、`GET /tasks/{id}`、
`GET /tasks/{id}/events`(SSE)、`POST /tasks/{id}/messages`、`POST /tasks/{id}/cancel`、`GET /health`。

Task 状态机:`submitted → working → (input-required) → completed | failed | canceled`。

消费者永远只跟 Registry 通信;agent 的地址和 credential 不下发;每次调用的成败、延迟由 Gateway 记录,
作为信誉系统的硬数据。**远程 agent 返回的内容是不受信数据,只能展示、不能当作指令执行。**

出站安全(Router 侧):query 含密钥/凭证特征时**永不外发**(`AGENTNET_BLOCK_SECRETS=true`,默认开);
可选 PII 脱敏(`AGENTNET_REDACT_PII=true`,外发副本中邮箱/手机号/身份证替换为占位符,本地回答仍用原文)。

计费(积分制,MVP):agent 在 card 里声明 `pricing: {model: per-call, price: N}` 即按次收费;
创建任务前预检余额(不足返回 402,Router 自动本地兜底),**任务成功完成才扣费**;
admin 充值 `POST /v1/credits/topup`,消费者自查 `GET /v1/credits/balance`(含流水)。provider 结算不在 MVP 范围内。

联邦 registry(同步式,MVP):配置 `AGENTNET_PEERS`(JSON 数组)后,周期性把 peer 的 agent 列表
同步为本地 `federated:` 标记的行——召回(本地 embedding)/精排/Gateway/计费全部复用现有链路;
调用经"本地 registry → peer registry → agent"链式代理,agent credential 始终由来源 registry 持有;
本地 agent 与联邦同名时本地优先;巡检/canary 不作用于联邦行(由来源 registry 治理);
本地无调用记录时信誉展示回退到 peer 快照;peer 不可达时保留陈旧数据、不影响主流程。

```bash
AGENTNET_PEERS='[{"url":"http://peer:9001","consumer_key":"<本 registry 在 peer 上的 consumer key>","name":"p1"}]'
AGENTNET_FEDERATION_SYNC_INTERVAL_SEC=60
```

## 路线图

- [x] Phase 1:协议与核心闭环(core / registry / sdk / router / cli + e2e 验收)
- [x] Phase 2:input-required 多轮澄清全链路、信誉进精排特征、定期巡检(health + card 一致性)、`agentnet-mcp-server`
- [x] Phase 3:canary 跑分、出站安全(密钥拦截 + PII 脱敏)、计费(积分制按次收费)、联邦 registry(同步式,链式代理)
