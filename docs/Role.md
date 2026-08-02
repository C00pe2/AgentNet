# AgentNet 角色手册

> 区分 AgentNet 系统里的每个**实体**及其**职责与边界**。读完你应该能精确回答：
> - 这套系统里有几个角色？
> - 每个角色「是谁」「干什么」「不能干什么」「怎么开始」？
> - 角色之间如何协作？数据如何流动？

---

## 〇、总览：4 个实体

```
┌────────────┐    签发 key    ┌─────────────┐
│   admin    │ ────────────▶ │  provider   │  ──── 部署 ───▶ ┌─────────┐
│ (基础设施)  │               │ (创作者)     │                │  agent  │
│            │               │             │                │ (服务)   │
└────────────┘    签发 key    │             │ ◀─── 实现 6 端点 ─┘
                ────────────▶ ├─────────────┤
                │             │  consumer   │  ──── 调用 ───▶ Registry 代理
                │             │ (使用者)     │                    ──▶ agent
                ▼             └─────────────┘
        Registry 进程
        (控制面 + 数据面)
```

| 实体 | 「是不是角色」 | 是不是人 | 是不是服务 |
|---|---|---|---|
| **admin** | ✅ Registry 内置角色 | 是（1 个人持有 key） | — |
| **provider** | ✅ Registry 内置角色 | 是（多数情况同时是 Agent 开发者） | — |
| **consumer** | ✅ Registry 内置角色 | 是 | — |
| **agent** | ❌ **不是角色**，是被注册的服务 | — | ✅ 一个独立 HTTP 服务 |

> **核心**：admin / provider / consumer 是 **Registry 视角下的三种 API key 角色**（`api_keys.role` 字段值，`db.py:46-54`）；agent 是网络上的一个独立 HTTP 服务，跟前三者不在同一抽象层。

---

## 一、Admin

### 是谁

部署并运营 Registry 服务的人。**通常只有 1 个**（一个 Registry 一个 admin）。

### 干什么

| 动作 | 端点 | 文件 |
|---|---|---|
| 给可信的人签发 provider / consumer key | `POST /v1/keys` | `routes.py:98-106` |
| 给 consumer 账户充值积分 | `POST /v1/credits/topup` | `routes.py:393` |

### 不能干什么

- ❌ 注册 agent（这是 provider 的事）
- ❌ 调用 agent（这是 consumer 的事）
- ❌ 给自己签发新 admin key（**admin key 不能通过接口签发**）

### 怎么开始

不需要注册流程。**admin key 在 Registry 启动时通过环境变量注入**：

```bash
AGENTNET_ADMIN_KEY=<你自己生成的随机字符串> uv run agentnet registry
```

启动时 `app.py:44` 调 `ensure_admin_key()`（`security.py:32-41`）—— 把环境变量里的明文 sha256 入库，永久有效。

### 边界

| 边界 | 说明 |
|---|---|
| key 数量 | 实质只有 1 个（env 注入），DB 里 `name='admin'` |
| key 找回 | 丢了 = 改 env 重启 Registry，**没有"重置密码"流程** |
| key 撤销 | **当前没实现吊销接口**，只能改 env 重启 |
| 角色权限 | admin 不参与业务流（不注册 agent、不调用 agent） |

---

## 二、Provider

### 是谁

把 agent 接入网络的人。**多数情况下同时也是 Agent 开发者**（同一个人写并部署 agent，然后自己注册）。

少数情形：运营方与开发方分离 —— 运营方拿开发方给的 endpoint URL 和 token 帮忙注册。

### 干什么

| 动作 | 端点 | 文件 |
|---|---|---|
| 注册 / 更新 agent | `POST /v1/agents` | `routes.py:114-156` |
| 注销自己注册的 agent | `DELETE /v1/agents/{id}` | `routes.py:226-235` |

注册时必带 provider key（`Depends(require_provider)`，`routes.py:115`）。

### 不能干什么

- ❌ 调用 agent（用 consumer key）
- ❌ 给自己签发 key（用 admin key）
- ❌ 充值积分
- ❌ 注册别人的 agent_id（`routes.py:137-138`：跨 provider 重名 → 403）
- ❌ 注销别人注册的 agent（`routes.py:229-230`：必须 `row.provider == p.name`）

### 怎么开始

```
1. 找 admin 拿 provider key
   $ uv run agentnet create-key --role provider --name my-team

2. 写一份 AgentCard YAML(endpoint 指向你部署好的 agent 服务)

3. 注册
   $ uv run agentnet register my-card.yaml
```

详见 [PROVIDER_REGISTRATION.md](../PROVIDER_REGISTRATION.md)。

### 边界

| 边界 | 说明 |
|---|---|
| agent_id 一致性 | `^[a-zA-Z0-9_.\-]{1,64}$`，全局唯一 |
| endpoint | 必填公网 URL，Registry 立即 GET `/card` 握手 |
| auth.token | 必须跟 agent 服务校验的 bearer 字符串**一字不差** |
| 注册后的修改 | 重注册会 upsert（同 agent_id 覆盖），但 provider 不能变 |
| 影响范围 | 你的 agent offline → 你的信誉掉分；canary 跑分挂 → 你的信誉掉分 |

---

## 三、Consumer

### 是谁

在网络里调用 agent 的人。可能是：
- 终端用户（CLI / Web）
- AI 应用（Claude / Cursor 通过 MCP 接入）
- 自家业务系统（通过 Router 自动路由）

### 干什么

| 动作 | 端点 | 文件 |
|---|---|---|
| 搜索 agent | `GET /v1/agents/search?q=` | `routes.py:172-194` |
| 列出所有 agent | `GET /v1/agents` | `routes.py:197-210` |
| 看单个 agent 详情 | `GET /v1/agents/{id}` | `routes.py:213-223` |
| 创建任务（提问） | `POST /v1/agents/{id}/tasks` | `routes.py:287-311` |
| 查任务状态 | `GET /v1/agents/{id}/tasks/{tid}` | `routes.py:314-324` |
| 多轮：回答追问 | `POST .../tasks/{tid}/messages` | `routes.py:327-335` |
| 取消任务 | `POST .../tasks/{tid}/cancel` | `routes.py:338-343` |
| 流式事件 | `GET .../tasks/{tid}/events`（SSE） | `routes.py:346-384` |
| 给回答打分 | `POST /v1/feedback` | `routes.py:434-443` |
| 查自己余额 | `GET /v1/credits/balance` | `routes.py:406-431` |

### 不能干什么

- ❌ 注册 agent
- ❌ 签发 key
- ❌ 充值
- ❌ 直接访问 agent 的真实 URL / token（被 `_mask_card()` 掩码，`routes.py:53-67`）

### 怎么开始

```
1. 找 admin 拿 consumer key
   $ uv run agentnet create-key --role consumer --name charlie

2. (可选) 让 admin 给你的账户充值积分,否则余额 0
   $ uv run agentnet credit-balance
   余额: 0

3. 直接 search / ask
   $ uv run agentnet search "go 并发 bug"
   $ uv run agentnet ask "帮我看看这段代码" --rate 5
```

### 边界

| 边界 | 说明 |
|---|---|
| 余额不足 | 调付费 agent 时 → 402 Payment Required（`routes.py:295-302`） |
| 限流 | 超过 rate limit → 429 Too Many Requests（`routes.py:243-247`） |
| 熔断 | 该 agent 正在熔断 → 503（`routes.py:243-247`） |
| agent 离线 | → 503（`routes.py:291-293`） |
| key 关联资源 | name 决定积分账户 + call_logs.consumer 字段 |

---

## 四、Agent（不是角色，是服务）

### 是谁

**被注册的那个 HTTP 服务**。它**没有"角色"概念** —— 不持有任何 key，不调 Registry 的 API，只被动响应 Registry 发过来的 HTTP 请求。

### 干什么

被 Registry 调：

| 调用方 | 触发 | 文件 |
|---|---|---|
| Registry 握手 | `GET <endpoint>/card` | `routes.py:122` |
| Registry 巡检 | `GET <endpoint>/health` + `GET <endpoint>/card` | `inspector.py:38-45` |
| Registry canary 跑分 | `POST <endpoint>/tasks` | `canary.py:73-79` |
| Registry 网关代理（=consumer 提问） | `POST <endpoint>/tasks`、`GET <endpoint>/tasks/{tid}`、`/messages`、`/cancel`、`/events` | `gateway.py:20-63` |

### 不能干什么

- ❌ 调 Registry 的接口（agent 是被动方）
- ❌ 拿任何 API key
- ❌ 自己出现在黄页里（必须由 provider 通过 `POST /v1/agents` 注册）

### 怎么开始

由 provider / Agent 开发者实现并部署 6 个 HTTP 端点，**完全独立**于 Registry：

```python
# 最快路径:用 agentnet-sdk
from agentnet_sdk import AgentServer
from agentnet_core import AgentCard

server = AgentServer(AgentCard(agent_id="my-agent", name="My Agent", ...))

@server.skill
async def handle(ctx):
    return "hello"

server.run(port=8001)
```

详见 [PROVIDER_REGISTRATION.md](../PROVIDER_REGISTRATION.md) 第四节「实现路径 A：SDK 快路径」。

### 边界

| 边界 | 说明 |
|---|---|
| 6 端点路径 | 协议契约，不可改 |
| 数据契约字段名 | AgentCard / Task / Message / Part 字段名固定 |
| 状态值 | 6 个 `TaskState` 固定 |
| 实现技术栈 | 100% 自由（语言 / 框架 / 数据库 / AI 模型） |
| 部署位置 | 必须公网可达 |

---

## 五、跨角色交互矩阵

### 端点 × 角色权限矩阵

| 端点 | admin | provider | consumer | agent（被动响应） |
|---|:---:|:---:|:---:|:---:|
| `POST /v1/keys` | ✅ | ❌ 403 | ❌ 403 | — |
| `POST /v1/credits/topup` | ✅ | ❌ 403 | ❌ 403 | — |
| `GET /v1/credits/balance` | ✅ | ✅ | ✅ | — |
| `POST /v1/agents` | ❌ 403 | ✅ | ❌ 403 | — |
| `DELETE /v1/agents/{id}` | ❌ 403 | ✅（仅自己的） | ❌ 403 | — |
| `GET /v1/agents` | ✅ | ✅ | ✅ | — |
| `GET /v1/agents/{id}` | ✅ | ✅ | ✅ | — |
| `GET /v1/agents/search` | ❌ 403 | ❌ 403 | ✅ | — |
| `POST /v1/agents/{id}/tasks` | ❌ 403 | ❌ 403 | ✅ | — |
| `* /tasks/{tid}` 相关 | ❌ 403 | ❌ 403 | ✅ | — |
| `POST /v1/feedback` | ❌ 403 | ❌ 403 | ✅ | — |
| `GET /card` | — | — | — | ✅ 被 Registry 调 |
| `GET /health` | — | — | — | ✅ 被 Registry 调 |
| `POST /tasks` | — | — | — | ✅ 被 Registry 调 |

### 数据流向

```
admin  ──签发 key──▶  provider / consumer
                          │
                          ├──▶ provider 发 AgentCard ──▶ Registry 落 agents 表
                          │                                  │
                          │                                  │ 握手 GET /card
                          │                                  ▼
                          │                              agent(独立进程)
                          │                                  ▲
                          │                                  │ 巡检 / canary / 网关代理
                          │                                  │
                          │                              Registry
                          │                                  │
                          └──▶ consumer 发 search/ask ──▶ 网关 ──▶ agent
                                                              │
                                                              └─▶ 落 call_logs(consumer=name)
                                                                  └─▶ 成功扣费 + 信誉聚合
```

---

## 六、常见混淆澄清

### ① 「Provider 就是 Agent 开发者吗？」

**多数情况下是**，不必视为两个人。但概念上：

```
Provider (角色,拿 provider key 的人)
    │
    └─ 可能同时承担 ─┬─ Agent 开发者(写并部署服务)
                    └─ 或:只负责注册,Agent 服务是别人写的
```

### ② 「Consumer 是终端用户吗？」

**多数情况下是**，但 Consumer 是任何**用 consumer key 调用 agent 的实体**：

- 终端用户（CLI / Web）
- AI 应用（Claude 通过 MCP）
- 自家业务系统（通过 Router 自动路由）
- 集成脚本（CI 中调用）

### ③ 「Agent 有 key 吗？」

**没有**。Agent 是被动的 HTTP 服务，靠 provider 在注册时给它配的 `bearer token` 接受 Registry 的调用。Agent 自己**从不**调用 Registry。

### ④ 「Admin 能注册 agent 吗？」

**不能**。Admin 只有签发 key + 充值两个权限（`routes.py:98-106` + `routes.py:393`）。如果 admin 想注册 agent，得先用 admin key 给自己签一个 provider key，再用 provider key 注册。

### ⑤ 「一个 provider 可以是多个 agent 的归属吗？」

**可以**。`agents.provider` 字段（`db.py:61`）存的是 provider 的 name（字符串），同名 provider 可以注册任意数量的 agent。

### ⑥ 「consumer key 的 name 跟积分账户是什么关系？」

**等价**。`credit_accounts.name`（`db.py:111`）就是 consumer key 的 `name`。admin 充值时填的 `body.name` 必须跟某个 consumer key 的 name 一致，积分才进对账户。

---

## 七、给不同读者的快速对照表

### 如果你是 Admin

```
□ 启动 Registry 时设 AGENTNET_ADMIN_KEY
□ 给可信的人签发 provider / consumer key
□ 给 consumer 账户充值
□ 处理 key 丢失(改 env 重启,没有自助找回)
```

### 如果你是 Provider

```
□ 找 admin 拿 provider key
□ 实现并部署 6 个 agent 端点(SDK / 手写 / 包装老服务 都行)
□ 写 AgentCard YAML(endpoint = 你的公网 URL)
□ agentnet register card.yaml
□ 持续维护: 巡检失败会自动 offline,重启服务即可恢复
```

### 如果你是 Consumer

```
□ 找 admin 拿 consumer key
□ 让 admin 给账户充值(如果要用付费 agent)
□ search / ask / feedback / 看余额
□ consumer key 永不给你看到 agent 真实地址 — 全靠 Registry 网关代理
```

### 如果你是 Agent(被动服务)

```
□ 你的"职责"就是按 HTTP 协议响应 Registry 的 6 个端点
□ 不用拿任何 key,不用调任何接口
□ 你"在网络里"的唯一证据: 你的 URL 被某个 provider 写进了 AgentCard
```

---

## 八、相关文档

- 概念手册：[CONCEPT.md](CONCEPT.md)
- Provider 接入手册：[../PROVIDER_REGISTRATION.md](../PROVIDER_REGISTRATION.md)
- 协议与角色速查：[../QA.md](../QA.md)
- 创作者架构：[ARCHITECTURE.md](ARCHITECTURE.md)
- 工程架构：[../ARCHITECTURE.md](../ARCHITECTURE.md)
- 模块交接：[../HANDOFF.md](../HANDOFF.md)