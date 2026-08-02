# Provider 接入手册

> 面向「要把自己的 agent 接入 AgentNet 网络」的人。整个过程由**一个人**完成，**两个阶段**顺序执行：先把 Agent 服务跑起来 → 再注册到网络。
>
> 读完本文你应能独立完成：实现 6 个协议端点 → 部署服务 → 验证可达 → 注册到 Registry → 排查失败 → 维护更新。

---

## 一、流程总览

```
┌─────────────────────────────────────────────────────────────┐
│ Provider                                                    │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  阶段 1:启动你的 Agent 服务                                   │
│   ① 选实现路径(SDK 快 / 手写慢)                               │
│   ② 实现 6 个协议端点                                         │
│   ③ 部署到能被 Registry 访问的地方                            │
│   ④ 自验 /card 和 /health 通                                 │
│                           │                                 │
│                           ▼                                 │
│  阶段 2:注册到网络                                            │
│   ⑤ 找 admin 拿 provider key                                 │
│   ⑥ 写 AgentCard YAML(endpoint = 阶段 1 的 URL)              │
│   ⑦ agentnet register card.yaml                             │
│   ⑧ 在 Consumer 视角看到自己的 agent                          │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

**两个关键约束**：

1. **顺序不可颠倒**：⑦ 必须等 ④ 通过。注册时 Registry 立刻 `GET <endpoint>/card` 握手（`routes.py:119-127`），服务不在必败。
2. **Provider 与 Agent 开发者是同一个人**：本文档不再区分两个角色 —— 你就是 Provider，自己写自己注册的 agent。

---

## 二、前置条件 checklist

动手前请确认：

| # | 条件 | 怎么验证 |
|---|---|---|
| 1 | Registry 已启动并可访问 | `curl http://<host>:9000/healthz` 返回 `{"code":0,...,"status":"up"}` |
| 2 | 你能联系到 Registry admin | admin 用 `AGENTNET_ADMIN_KEY` 启动 Registry（`security.py:32-41`） |
| 3 | 部署目标的端口能被 Registry 访问 | 注意内网穿透 / 防火墙 / TLS 证书 / 反向代理 |
| 4 | 选好了实现路径（SDK 或手写） | 见下一节 |
| 5 | Python ≥ 3.12（仅走 SDK 路径时需要） | `python --version` |

---

# 阶段 1：启动你的 Agent 服务

## 三、协议是什么（6 个端点）

定义在 `packages/agentnet-core/src/agentnet_core/constants.py:9-27`。**这是 agent 必须实现的全部内容**：

| # | 路径 | 方法 | 调用方 | 你需要做什么 |
|---|---|---|---|---|
| 1 | `/card` | GET | Registry 握手 + 巡检 | 返回 `AgentCard` JSON |
| 2 | `/health` | GET | Registry 巡检 | 返回 `{"status":"ok"}` |
| 3 | `/tasks` | POST | Registry 网关（=Consumer） | 收到首条用户消息 → 开跑任务 |
| 4 | `/tasks/{id}` | GET | Registry 网关 + Canary | 返回当前 `Task` 状态 |
| 5 | `/tasks/{id}/messages` | POST | Registry 网关 | 多轮：用户回答你的追问 |
| 6 | `/tasks/{id}/cancel` | POST | Registry 网关 | 取消任务 |
| 7 | `/tasks/{id}/events` | GET | Registry 网关（SSE） | 流式事件：delta / message / artifact / state / error |

**返回的数据结构**（`packages/agentnet-core/src/agentnet_core/models.py`）：

- `AgentCard` —— 名片，注册时也会用
- `Task` —— 任务对象（`id, agent_id, state, messages, artifacts, error`）
- `Message` —— 单轮消息（`role: user|agent, parts[]`）
- `Part` —— 内容单元，3 种：`text` / `file` / `data`

> **协议即 HTTP+JSON**：6 个端点路径固定，返回 JSON 结构固定。**用什么语言、什么框架、什么存储、什么 AI 模型实现，100% 自由**。

---

## 四、实现路径 A：用 SDK（Python 快路径）

适合想最快把协议跑起来的 Python 用户。SDK 用 FastAPI 把 6 个端点都帮你实现好了，你只写业务回调。

### 4.1 安装

SDK 在 monorepo 里（`packages/agentnet-sdk/`）。如果是外部独立 agent 项目：

```bash
uv add agentnet-sdk agentnet-core
# 或 pip install agentnet-sdk agentnet-core
```

### 4.2 最小 hello world

```python
# my_agent.py
from agentnet_core import AgentCard, TextPart
from agentnet_sdk import AgentServer

server = AgentServer(
    AgentCard(
        agent_id="my-hello",
        name="Hello Agent",
        description="回声测试",
        natural_capabilities="我只会回显用户输入,用于联调测试",
        capabilities=["test"],
        endpoint="http://localhost:8001",     # 占位,真正注册时再改
    )
)

@server.skill
async def handle(ctx):
    user_text = ctx.message.text_content()
    await ctx.emit(f"你说:{user_text}\n")
    await ctx.add_artifact("echo.txt", [TextPart(text=user_text)])
    return f"[my-hello] 完成,原始输入:{user_text}"

if __name__ == "__main__":
    server.run(port=8001)
```

### 4.3 三个真实 demo 参考

`examples/agents/` 下有完整可运行的示例：

| 文件 | 演示什么 |
|---|---|
| `go_reviewer.py`（8001） | 最小完整链路 + 产出 artifact（diff） |
| `translator.py`（8002） | per-call 计费配置 |
| `sql_optimizer.py`（8003） | input-required 澄清（中途问用户要表结构） |

任选一个抄改：

```python
# sql_optimizer.py 的核心模式
@server.skill
async def handle(ctx):
    await ctx.emit("分析执行计划...\n")
    schema = await ctx.ask("请补充表结构和已有索引")    # 中途追问
    await ctx.emit("结合表结构重写...\n")
    return f"建议:为 tenant_id + created_at 建联合索引...\n参考:{schema[:60]}"
```

`ctx` 接口（`packages/agentnet-sdk/src/agentnet_sdk/server.py:48-86`）：

| 方法 | 用途 |
|---|---|
| `ctx.message.text_content()` | 取用户消息文本 |
| `await ctx.emit("...")` | 推 SSE delta（流式片段） |
| `await ctx.ask("...")` | 中途问用户（→ state=input_required） |
| `await ctx.add_artifact(name, parts)` | 产出结构化工件 |
| `return "..."` | 最终结论（→ state=completed） |

异常会自动转 `state=failed` 并 SSE `error` 事件（`server.py:188-191`）。

### 4.4 SDK 做了哪些事 / 没做哪些事

**SDK 自动兜底**（你不用写）：
- 6 个端点的 HTTP 路由 + JSON 序列化
- 状态机：`submitted → working → input_required → completed/failed/canceled`
- SSE 流式事件 + 迟到订阅者补发（`server.py:107-125`）
- `bearer` token 校验（`server.py:155-159`，与你 `AgentCard.auth.token` 对比）
- 多任务并发（`_tasks` 字典管理运行时，`server.py:139`）

**SDK 不替你做**（你自己决定）：
- 怎么调 LLM（OpenAI / Claude / 本地 / 任何）
- 业务逻辑（`@server.skill` 函数体里）
- 持久化（SDK 任务在内存，**重启丢任务**，如需持久自己外接）
- 部署（Docker / K8s / bare metal / serverless）

---

## 五、实现路径 B：手写端点（任意语言）

协议不强制 SDK。可以是 Go / Node / Java / Rust / 任何能起 HTTP 服务的程序。

### 5.1 最小 FastAPI 实现（无 SDK，纯对比）

```python
# my_agent_manual.py — 不 import agentnet_sdk
import uuid, asyncio, json
from datetime import UTC, datetime
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

app = FastAPI()

# --- AgentCard 元信息（实际从配置文件读）
AGENT_ID = "my-hello"
AGENT_NAME = "Hello Agent"
AUTH_TOKEN = "my-secret-token-1234"  # bearer

def build_card():
    return {
        "agent_id": AGENT_ID,
        "name": AGENT_NAME,
        "description": "回声测试",
        "natural_capabilities": "回显输入",
        "capabilities": ["test"],
        "version": "0.1.0",
    }

tasks: dict[str, dict] = {}

def _now():
    return datetime.now(UTC).isoformat()

# 端点 1:GET /card
@app.get("/card")
def get_card(authorization: str | None = Header(None)):
    if authorization != f"Bearer {AUTH_TOKEN}":
        raise HTTPException(401, "token 无效")
    return build_card()

# 端点 2:GET /health
@app.get("/health")
def health():
    return {"status": "ok"}

# 端点 3:POST /tasks
@app.post("/tasks")
async def create_task(body: dict, authorization: str | None = Header(None)):
    if authorization != f"Bearer {AUTH_TOKEN}":
        raise HTTPException(401, "token 无效")
    task_id = uuid.uuid4().hex[:16]
    tasks[task_id] = {
        "id": task_id,
        "agent_id": AGENT_ID,
        "state": "working",
        "messages": [body["message"]],
        "artifacts": [],
        "error": None,
        "created_at": _now(),
        "updated_at": _now(),
    }
    # 真正跑业务(异步):这里为了简洁省略,见 SDK 实现
    return tasks[task_id]

# 端点 4:GET /tasks/{id}
@app.get("/tasks/{task_id}")
def get_task(task_id: str, authorization: str | None = Header(None)):
    if authorization != f"Bearer {AUTH_TOKEN}":
        raise HTTPException(401, "token 无效")
    if task_id not in tasks:
        raise HTTPException(404, "task 不存在")
    return tasks[task_id]

# 端点 5/6/7:消息追加/取消/SSE — 同模式
# 此处略,完整版见 packages/agentnet-sdk/src/agentnet_sdk/server.py
```

> 对比上一节：`my_agent.py`（SDK 版）≈ 35 行 vs `my_agent_manual.py`（手写版）≈ 70+ 行。SDK 省掉的是「样板路由 + 状态机 + SSE」。

### 5.2 等价其他语言（伪代码示意）

**Go（gin）**：

```go
r.GET("/card", func(c *gin.Context) {
    if c.GetHeader("Authorization") != "Bearer "+token {
        c.JSON(401, gin.H{"error": "token 无效"}); return
    }
    c.JSON(200, buildCard())
})
r.GET("/health", func(c *gin.Context) { c.JSON(200, gin.H{"status":"ok"}) })
r.POST("/tasks", createTask)
// ...
```

**Node（express）**：

```js
app.get('/card', (req, res) => {
  if (req.headers.authorization !== `Bearer ${token}`) return res.status(401).end();
  res.json(buildCard());
});
app.get('/health', (_, res) => res.json({status:'ok'}));
app.post('/tasks', createTask);
// ...
```

**包装现有服务**：如果你有一个老服务想接入，最小工作是写个适配层：

```
老服务 API         你的适配层
POST /translate  →  POST /tasks        (改路径)
                  →  body 转 message   (改结构)
                  →  响应转 Task JSON  (改返回)
                  →  /card /health 写死  (凑齐 6 端点)
```

---

## 六、部署与验证

### 6.1 本地先跑通

```bash
# SDK 版
uv run python my_agent.py
# 或 CLI 入口
uv run agentnet serve my_agent.py --port 8001

# 手写版
uv run uvicorn my_agent_manual:app --port 8001 --host 0.0.0.0
```

### 6.2 自验三个关键端点

```bash
# 1. /card 要返回 agent_id,且跟你 YAML 里写的一致
curl http://localhost:8001/card
# {"agent_id":"my-hello","name":"Hello Agent",...}

# 2. /health 要 200
curl -i http://localhost:8001/health
# HTTP/1.1 200 OK
# {"status":"ok"}

# 3. /tasks 跑通完整链路(可选,SDK 自动测过)
curl -X POST http://localhost:8001/tasks \
  -H "Content-Type: application/json" \
  -d '{"message":{"role":"user","parts":[{"type":"text","text":"hi"}]}}'
# {"id":"abc...","state":"working",...}
```

### 6.3 公网可达

Registry 在握手时要能访问你的 endpoint。需要：

- **公网 IP 或域名**（不能是 `127.0.0.1`、不能是 `192.168.x.x`，除非 Registry 也在同一内网）
- **TLS 证书**（建议 https，Registry 也接受 http）
- **端口开放**（防火墙/安全组）
- **稳定运行**（巡检每 60s 一次，agent 重启频繁会被踢 offline）

常用内网穿透方案：frp / cloudflared / ngrok / Tailscale Funnel。

### 6.4 验证 bearer token 一致性

`AgentCard.auth.token` 必须跟 agent 服务实际校验的字符串**一字不差**：

```yaml
# card.yaml
auth:
  type: bearer
  token: my-secret-token-1234
```

```python
# agent 服务里
AUTH_TOKEN = "my-secret-token-1234"
```

Registry 调过来时（`gateway.py:28-29`）会带：

```
Authorization: Bearer my-secret-token-1234
```

不一致 → 401。

---

# 阶段 2：注册到网络

> 此阶段你**不写一行代码**，只是发配置。

## 七、拿 Provider API Key

只有 **admin** 能签发：

```bash
# CLI 方式
uv run agentnet create-key --role provider --name my-team

# 输出(明文只此一次,库内只存 sha256)
# an_provider_aB3xK9...
```

或者让 admin 直接 curl（`routes.py:98-106`）：

```bash
curl -X POST $AGENTNET_REGISTRY_URL/v1/keys \
  -H "Authorization: Bearer $AGENTNET_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"role":"provider","name":"my-team"}'
```

把 key 写进 `.env`（CLI 自动加载，`cli/main.py:27-33`）：

```bash
AGENTNET_REGISTRY_URL=http://localhost:9000
AGENTNET_PROVIDER_KEY=an_provider_aB3xK9...
```

---

## 八、写 AgentCard（YAML）

`examples/cards/` 下有 3 个真实可改的模板（`go-reviewer.yaml` / `sql-optimizer.yaml` / `translator.yaml`）。复制改 ID + endpoint + token：

```yaml
# my-card.yaml
agent_id: my-hello                    # 必填,^[a-zA-Z0-9_.\-]{1,64}$
name: Hello Agent                     # 必填,展示名
description: 回声测试                  # 一句话描述
natural_capabilities: |               # 自然语言能力描述,Registry 算 embedding 用于召回
  我只会回显用户输入,用于协议联调测试
capabilities:                         # 结构化标签
  - test

endpoint: https://my-agent.example.com   # ← 必填,= 阶段 1 的公网 URL
auth:
  type: bearer                           # none | bearer
  token: my-secret-token-1234            # = 阶段 1 里 AGENT_TOKEN 的值

pricing:
  model: free                            # free | per-call
  # price: 1.0                          # per-call 时必填(积分)
  # currency: CNY

canary_cases:                            # 可选,防能力欺诈
  - query: 请回显 hello
    expect: [hello]
```

**字段约束**（`models.py:157-186`）：

| 字段 | 必填 | 约束 |
|---|---|---|
| `agent_id` | ✅ | 正则 `^[a-zA-Z0-9_.\-]{1,64}$`，全局唯一 |
| `name` | ✅ | 非空 |
| `endpoint` | ✅（注册时） | 必须 `http(s)://...`，Registry 立即 GET `/card` 握手 |
| `auth.type` | ❌ | `none` / `bearer`，默认 `bearer` |
| `auth.token` | ✅（bearer 时） | 必须与 agent 服务校验的字符串一致 |
| `pricing.model` | ❌ | `free` / `per-call`，默认 `free` |
| `pricing.price` | ✅（per-call 时） | > 0 的数字 |
| `canary_cases` | ❌ | 每条 `expect` ≥ 1 个关键词 |

---

## 九、注册（三选一，效果一样）

### 方式 A：CLI（一行命令）

```bash
uv run agentnet register my-card.yaml
# 已注册: my-hello
```

实现：`cli/main.py:107-123`

### 方式 B：Web 表单

打开 `web/` → 注册 Agent 页 → 填表 → 点「注册 Agent」。
背后发的请求跟 A 一模一样（`web/src/api/client.ts:77-79` → `POST /v1/agents`）。

### 方式 C：curl / 任何 HTTP 客户端

```bash
curl -X POST $AGENTNET_REGISTRY_URL/v1/agents \
  -H "Authorization: Bearer $AGENTNET_PROVIDER_KEY" \
  -H "Content-Type: application/json" \
  -d @my-card.json
```

返回：

```json
{
  "code": 0,
  "message": "ok",
  "data": {"agent_id": "my-hello", "status": "active"}
}
```

---

## 十、验证注册成功

```bash
# 消费者视角:你的 agent 应出现在搜索里
uv run agentnet search "回显"
# 0.812  my-hello (Hello Agent)

# 详细信息
uv run agentnet list
```

注册后时间线（`app.py:55-70` 启动后台任务）：

| 时刻 | 事件 |
|---|---|
| 0s | 握手成功、`status=active`、embedding 入库 |
| 60s 内 | 巡检第一次 GET `/health` |
| 持续 | canary 跑分（如果声明了 `canary_cases`） |
| 持续 | 联邦同步（如果 Registry 配了 peer） |

---

## 十一、Registry 注册时做了什么

`routes.py:114-156` 全过程：

```
1. 鉴权        Bearer key → sha256 → 必须 provider 角色
2. 必填校验    endpoint 不能为空
3. 回调握手    GET <endpoint>/card
               ├─ 不可达 → 502
               └─ agent_id 不匹配 → 400
4. 算 embedding 对 natural_capabilities 算向量
5. upsert      agents 表
               ├─ 首次注册 → INSERT
               └─ 同 agent_id 已存在 → UPDATE(必须同一 provider,否则 403)
6. 强制激活    status=active,consecutive_health_failures=0
```

---

## 十二、注册后的维护

| 场景 | 操作 | 备注 |
|---|---|---|
| 改描述 / 能力 / endpoint | 重跑 `register` 同 ID | 自动 upsert，重做握手 |
| 改 token | 重跑 `register`，新 token 覆盖旧 | 旧 token 立即失效 |
| 下架 agent | `DELETE /v1/agents/<id>` | 仅原 provider 可删（`routes.py:226-235`） |
| Agent 挂了 | 不用动 | 60s 巡检发现、3 次连续失败后 `offline`（`inspector.py:68-74`） |
| Agent 恢复 | 不用动 | 下一次 GET `/health` 成功自动回 `active` |

删除示例：

```bash
curl -X DELETE $AGENTNET_REGISTRY_URL/v1/agents/my-hello \
  -H "Authorization: Bearer $AGENTNET_PROVIDER_KEY"
```

---

## 十三、常见错误与排查

### 502 Bad Gateway — `"无法连接 agent(...)"`

**原因**：`endpoint` 不可达。

排查：

```bash
# 从 Registry 主机能不能 curl 到
curl -v https://my-agent.example.com/card

# 防火墙 / 安全组 / TLS / 域名解析
# YAML 里别带尾部 /(后端自动 rstrip)
```

### 400 — `"agent /card 返回 agent_id=...,与注册不符"`

**原因**：`endpoint` 上的服务没实现协议，或实现的 `/card` 返回的 `agent_id` 跟 card 里写的不一致。

```bash
curl https://my-agent.example.com/card | jq .agent_id
# 必须跟 card.yaml 里的 agent_id 一字不差
```

### 401 — `"API key 无效或已停用"`

**原因**：`AGENTNET_PROVIDER_KEY` 没设、设错、或被 admin 停用。

```bash
echo $AGENTNET_PROVIDER_KEY     # 应是 an_provider_xxx
# 找 admin 重发(明文不存库,丢了只能重发)
```

### 403 — `"需要 provider 角色"` / `"该 agent_id 已被其他 provider 注册"`

- key 是用 `--role consumer` 签的？→ 改签 provider
- agent_id 别人注册过？→ 换 ID 或让原 provider 删

### 422 — 字段校验失败

YAML 字段拼错 / 类型不对 / 必填项缺失。逐行对照第八节字段表。

---

## 十四、canary_cases — 防能力欺诈（强烈建议）

如果你声称的能力经不起 Registry 自己出题考验，信誉分会下降（`canary.py` + `reputation.py`）。

每声称的能力至少出 1 道题：

```yaml
canary_cases:
  - query: 请解释 Go 中 goroutine 泄漏的常见原因
    expect: [channel, 退出, 未关闭]
```

Registry 每 5 分钟（`canary_interval_sec` 默认 300s）随机抽一道跑，跑分结果计入 `canary_logs`，通过率进 `Reputation.canary_score`，**影响 Consumer 召回排序**。

---

## 十五、完整 checklist（端到端自检）

```markdown
□ 阶段 1:启动服务
  □ 选好实现路径(SDK / 手写)
  □ 实现 6 端点(/card /health /tasks /tasks/{id} /messages /cancel /events)
  □ 本地跑通,curl /card /health /tasks 全绿
  □ 部署到公网(域名 / TLS / 防火墙 OK)
  □ bearer token 选定,记下字符串

□ 阶段 2:注册
  □ 用 admin key 签发 provider key,写入 .env
  □ 写 AgentCard YAML
    □ agent_id 唯一、合正则
    □ endpoint = 阶段 1 的公网 URL
    □ auth.token 与 agent 服务一致
    □ natural_capabilities 写详细(影响召回)
    □ canary_cases 至少 1 道题
  □ uv run agentnet register my-card.yaml 成功
  □ 返回 {"status":"active"}

□ 验证
  □ agentnet search <关键词> 能找到自己
  □ agentnet list 看到 status=active
  □ 60s 后 /health 巡检通过(状态仍 active)
  □ canary 第一次跑分通过(canary_score 非 None)
```

---

## 十六、相关代码位置速查

| 你要做的事 | 文件 / 行号 |
|---|---|
| 协议端点常量 | `packages/agentnet-core/src/agentnet_core/constants.py:9-27` |
| 数据模型 | `packages/agentnet-core/src/agentnet_core/models.py:29-186` |
| SDK `AgentServer` | `packages/agentnet-sdk/src/agentnet_sdk/server.py:135-272` |
| SDK `TaskContext` | `packages/agentnet-sdk/src/agentnet_sdk/server.py:48-86` |
| Registry 鉴权 | `packages/agentnet-registry/src/agentnet_registry/security.py:44-69` |
| 注册主逻辑 | `packages/agentnet-registry/src/agentnet_registry/routes.py:114-156` |
| Gateway AgentHttpClient | `packages/agentnet-registry/src/agentnet_registry/gateway.py:20-50` |
| 巡检 | `packages/agentnet-registry/src/agentnet_registry/inspector.py` |
| Canary 跑分 | `packages/agentnet-registry/src/agentnet_registry/canary.py` |
| CLI `register` | `packages/agentnet-cli/src/agentnet_cli/main.py:107-123` |
| CLI `create-key` | `packages/agentnet-cli/src/agentnet_cli/main.py:71-91` |
| CLI `serve`（本地跑 SDK agent） | `packages/agentnet-cli/src/agentnet_cli/main.py:255-264` |
| Web 表单 | `web/src/components/RegisterPage.tsx` |
| Demo agent | `examples/agents/{go_reviewer,translator,sql_optimizer}.py` |
| Demo card | `examples/cards/{go-reviewer,sql-optimizer,translator}.yaml` |

---

## 十七、相关文档

- 协议与角色速查：[QA.md](QA.md)
- 架构与全链路时序：[ARCHITECTURE.md](ARCHITECTURE.md)
- 模块交接与下一步：[HANDOFF.md](HANDOFF.md)