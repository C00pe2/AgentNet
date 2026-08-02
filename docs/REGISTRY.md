# AgentNet Registry 模块文档

> 面向 **Registry 模块的开发者 / 运维者**。读完你应该能理解：
> - Registry 整体架构与每个文件承担的职责
> - 11 类功能如何协作
> - 14 个 HTTP 端点的完整清单与权限模型
> - 6 张数据表的结构与用途
> - 3 个后台任务的行为与可配项
> - 关键设计决策与边界

---

## 一、模块定位

Registry 是 AgentNet 的「中心枢纽」，由 FastAPI + SQLAlchemy + asyncio 后台任务实现，单进程承担：

| 角色 | 等价物 |
|---|---|
| 黄页 | Consul / 服务发现 |
| 调用网关 | Envoy / API Gateway |
| 信誉账本 | 银行征信 |
| 积分系统 | 支付宝 |
| 联邦节点 | 跨 registry 同步 |

**核心不变量**：

> 消费者永远只跟 Registry 通信；agent 的 endpoint 与 credential 不下发；每次调用的成败/延迟由 Gateway 亲眼记录——信誉、计费、canary 都长在这份硬数据上。

---

## 二、文件结构与功能分层

```
packages/agentnet-registry/src/agentnet_registry/
├── __init__.py        包元信息(__version__ = "0.1.0")
├── __main__.py        python -m agentnet_registry 入口
├── app.py             FastAPI 应用装配(lifespan + 中间件 + 后台任务编排)
├── config.py          Settings + PeerRegistry(pydantic-settings,AGENTNET_ 前缀)
├── db.py              引擎 + 6 张 ORM 表
├── security.py        API Key 鉴权(3 角色 + sha256 + 启动注入)
├── schemas.py         请求体 Pydantic 模型
├── routes.py          14 个 HTTP 端点(控制面 + 数据面 Gateway)
├── gateway.py         Agent 协议客户端(httpx 封装) + 调用记录 + 计费挂点
├── embedding.py       2 个 Embedder 实现 + 工厂 + card 文本拼接
├── recall.py          pgvector 余弦检索 + Python fallback
├── governance.py      RateLimiter + CircuitBreaker(每 agent 分桶)
├── reputation.py      从 call_logs + canary_logs 实时聚合
├── inspector.py       后台任务:周期巡检(/health + /card 一致性)
├── canary.py          后台任务:周期能力跑分(provider 自出题)
└── federation.py      后台任务:周期 peer registry 同步
```

按职责分 7 层：

```
┌─ 启动层 ─────────────────────────────────────────────┐
│  __main__.py  ·  config.py  ·  app.py                │
├─ 持久层 ─────────────────────────────────────────────┤
│  db.py(6 张表 + 引擎)                                │
├─ 鉴权层 ─────────────────────────────────────────────┤
│  security.py(3 角色 + sha256 + 启动注入)              │
├─ 协议/接口层 ────────────────────────────────────────┤
│  schemas.py  ·  routes.py  ·  gateway.py             │
├─ 检索层 ─────────────────────────────────────────────┤
│  embedding.py  ·  recall.py                         │
├─ 治理层 ─────────────────────────────────────────────┤
│  governance.py(限流+熔断) ·  reputation.py(信誉聚合)  │
├─ 后台任务层 ─────────────────────────────────────────┤
│  inspector.py  ·  canary.py  ·  federation.py        │
└──────────────────────────────────────────────────────┘
```

---

## 三、启动层

### 3.1 入口（`__main__.py:9-15`）

```python
def main() -> None:
    settings = Settings()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)
```

等价的 CLI 调用：`uv run agentnet registry`（`packages/agentnet-cli/src/agentnet_cli/main.py:55-63`）。

### 3.2 配置（`config.py:15-51`）

`Settings` 通过 pydantic-settings 从环境变量加载，前缀 `AGENTNET_`。**19 个可配项**：

| 类别 | 参数 | 默认 | 含义 |
|---|---|---|---|
| **基础** | `database_url` | `postgresql+asyncpg://...` | DB 连接 |
| | `host` / `port` | `0.0.0.0` / `9000` | 监听地址 |
| | `admin_key` | `changeme-admin-key` | 启动时自动入库的 admin key |
| **Embedding** | `embedding_backend` | `sentence-transformers` | 可切 `hash`(无模型环境) |
| | `embedding_model` | `BAAI/bge-m3` | 默认模型 |
| | `embedding_dim` | `1024` | 向量维度 |
| **召回** | `recall_top_k` | `10` | 召回候选数 |
| **超时** | `agent_timeout_sec` | `30.0` | 普通协议端点超时 |
| | `gateway_timeout_sec` | `300.0` | SSE 事件流超时 |
| **限流** | `rate_limit_rps` | `20` | 每 agent 每秒请求上限 |
| **熔断** | `breaker_fail_threshold` | `5` | 窗口内失败次数阈值 |
| | `breaker_open_sec` | `60.0` | 熔断持续秒数 |
| | `breaker_window_sec` | `10.0` | 熔断失败统计窗口 |
| **巡检** | `inspect_enabled` | `True` | 开关 |
| | `inspect_interval_sec` | `60.0` | 周期 |
| | `inspect_fail_threshold` | `3` | 连续失败次数阈值 |
| **Canary** | `canary_enabled` | `True` | 开关 |
| | `canary_interval_sec` | `300.0` | 周期 |
| | `canary_timeout_sec` | `30.0` | 单次跑分超时 |
| **联邦** | `peers` | `[]` | peer 列表(每项 `{url, consumer_key, name}`) |
| | `federation_enabled` | `True` | 开关 |
| | `federation_sync_interval_sec` | `60.0` | 同步周期 |

### 3.3 应用装配（`app.py:36-112`）

`create_app(settings)` 在 lifespan 内完成：

```
1. init_engine(settings.database_url)
2. init_db(engine)                 ← 自动 CREATE EXTENSION vector (Postgres)
3. ensure_admin_key(admin_key)     ← admin key 不存在则入库
4. 构建 app.state: settings / session_maker / http / embedder / rate_limiter / breaker
5. 启动 3 个后台任务: inspector / canary / federation
   (yield)
6. 关闭:stop 三个任务 → close http client → dispose engine
```

还注册：

- **中间件**：`RequestIdMiddleware`（`app.py:29-33`）—— 给每个响应加 `X-Request-Id`
- **3 个异常处理器**（`app.py:89-106`）：HTTPException / RequestValidationError / 未捕获异常 → 统一 envelope 格式 `{code, message, data}`
- **辅助端点**：`GET /healthz` → `{"code":0,...,"status":"up"}`

---

## 四、持久层（`db.py`，6 张表）

### 4.1 ORM 表结构

| 表 | 关键字段 | 用途 |
|---|---|---|
| **api_keys**（46-54） | `key_hash` (sha256, unique, indexed), `role` (`admin`/`provider`/`consumer`), `name`, `active`, `created_at` | API key 存储 |
| **agents**（57-77） | `agent_id` (PK), `provider`, `name`, `description`, `natural_capabilities`, `capabilities` (JSON), `canary_cases` (JSON), `endpoint`, `auth_type`, `auth_token`, `pricing` (JSON), `remote_reputation` (JSON, federated 来源快照), `version`, `status` (`active`/`offline`), `consecutive_health_failures`, `embedding` (VectorType), `created_at`, `updated_at` | Agent 注册信息 |
| **call_logs**（80-93） | `task_id` (unique, indexed), `agent_id` (FK), `consumer`, `created_at`, `finished_at`, `final_state`, `latency_ms`, `error`, `rating`, `charge` | Gateway 调用记录(信誉 + 计费数据源) |
| **canary_logs**（96-105） | `agent_id` (FK), `query`, `passed`, `detail` (未通过原因), `latency_ms`, `created_at` | canary 跑分记录 |
| **credit_accounts**（108-113） | `name` (PK, =consumer key 的 name), `balance`, `updated_at` | 消费者积分账户 |
| **credit_transactions**（116-124） | `name` (indexed), `delta` (+/-), `reason` (`topup`/`charge`), `task_id`, `created_at` | 积分流水 |

### 4.2 向量类型（`db.py:25-35`）

`VectorType` TypeDecorator：

- **PostgreSQL**：`pgvector.sqlalchemy.Vector(1024)` —— 利用 pgvector 索引加速
- **其他方言**（SQLite 等）：`JSON` 数组 —— 全量取回 Python 计算余弦

### 4.3 引擎与初始化（`db.py:135-156`）

```python
def init_engine(database_url: str) -> AsyncEngine:
    global _engine, _sessionmaker
    _engine = create_async_engine(database_url, pool_size=10, max_overflow=20)
    _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine

async def init_db(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        if engine.dialect.name == "postgresql":
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
```

进程级单例：`get_engine()` / `get_sessionmaker()`。

---

## 五、鉴权层（`security.py`）

### 5.1 三个角色

| 角色 | 拥有权限 | 不能做什么 |
|---|---|---|
| **admin** | 签 key、充值积分 | 注册 agent、调用 agent |
| **provider** | 注册 / 更新 / 注销 agent | 签 key、充值、调用 agent |
| **consumer** | 搜索 / 调用 / 评分 agent、查余额 | 注册 agent、签 key、充值 |

### 5.2 Key 的生命周期

```
Registry 启动(env:AGENTNET_ADMIN_KEY)
       │
       ▼ ensure_admin_key(security.py:32-41)
api_keys 表里自动入库:{key_hash=sha256(admin_key), role=admin, name=admin}
       │
       ▼ admin 调 POST /v1/keys {role, name}
generate_key(role) (security.py:23-24) → "an_<role>_<secrets.token_urlsafe(24)>"
       │
       ▼
入库 sha256(key)
       │
       ▼
明文一次性返回给 admin
       │
       ▼ admin 转交给 provider / consumer
后续每次请求 Authorization: Bearer <明文 key>
       │
       ▼ get_principal(security.py:44-55)
sha256(明文) → 查表 → 返 Principal(role, name)
```

### 5.3 三个角色闸门工厂

```python
def require_role(*roles: str):
    async def _dep(p: Principal = Depends(get_principal)) -> Principal:
        if p.role not in roles:
            raise HTTPException(403, f"需要 {' / '.join(roles)} 角色")
        return p
    return _dep

require_admin    = require_role("admin")      # security.py:67
require_provider = require_role("provider")  # 68
require_consumer = require_role("consumer")  # 69
```

**核心不变量**：`api_keys` 表里只存 sha256，**明文仅签发时返回一次**（`routes.py:105` 注释："明文仅此一次返回"）。

---

## 六、接口层（14 个端点）

### 6.1 控制面

| 端点 | 角色 | 行号 | 行为 |
|---|---|---|---|
| `POST /v1/keys` | admin | 98-106 | 签发 provider / consumer key |
| `POST /v1/agents` | provider | 114-156 | 注册 / 更新 agent(回调握手 + embedding + upsert) |
| `GET /v1/agents/search?q=&top_k=` | consumer | 172-194 | embedding 召回 top-K + 信誉填充 |
| `GET /v1/agents` | 任意 | 197-210 | 列表(含信誉 + status) |
| `GET /v1/agents/{id}` | 任意 | 213-223 | 详情(含信誉 + status) |
| `DELETE /v1/agents/{id}` | provider | 226-235 | 注销(仅原 provider) |
| `POST /v1/credits/topup` | admin | 392-403 | 给 consumer 账户充积分 |
| `GET /v1/credits/balance` | 任意 | 406-431 | 查余额(含最近 10 条流水) |
| `POST /v1/feedback` | consumer | 434-443 | 给 agent 评分(写 call_logs.rating) |

### 6.2 数据面 Gateway

| 端点 | 角色 | 行号 | 行为 |
|---|---|---|---|
| `POST /v1/agents/{id}/tasks` | consumer | 287-311 | 创建任务 → 限流 + 熔断 + 余额预检 + 落 call_log + 代理 POST /tasks |
| `GET /v1/agents/{id}/tasks/{tid}` | consumer | 314-324 | 代理 GET /tasks/{tid} + 终态挂点 |
| `POST .../tasks/{tid}/messages` | consumer | 327-335 | 多轮：回答 agent 追问 |
| `POST .../tasks/{tid}/cancel` | consumer | 338-343 | 取消 + 记 terminal |
| `GET .../tasks/{tid}/events` | consumer | 346-384 | SSE 透传 + 旁路记录终态 |

### 6.3 辅助

| 端点 | 行号 | 行为 |
|---|---|---|
| `GET /healthz` | app.py:108-110 | 健康检查 |

### 6.4 注册详细流程（`routes.py:114-156`）

```
1. 鉴权      require_provider
2. 必填校验  endpoint 不能为空
3. 回调握手  GET <endpoint>/card
             ├─ 不可达 → 502 Bad Gateway
             └─ agent_id 不匹配 → 400 Bad Request
4. 计算 embedding  对 natural_capabilities 算向量
5. upsert agents 表
             ├─ 首次注册 → INSERT
             └─ 同 agent_id 已存在:
                ├─ row.provider == p.name → UPDATE
                └─ row.provider != p.name → 403(跨 provider 重名)
6. 强制激活  status=active, consecutive_health_failures=0
```

### 6.5 消费者视角掩码（`routes.py:53-67`）

`_mask_card(row)` 返回给消费者的 card：

- `endpoint=""`(真实 URL 不下发)
- `auth.type="none"`(credential 不下发)
- 其他字段保留

### 6.6 联邦调用 envelope 解包（`routes.py:250-258`）

`federated` 行的下游是另一个 registry，需解开对方的 Envelope 协议：

```python
if payload["code"] != 0:
    raise HTTPException(502, f"peer registry 错误: {payload['message'][:200]}")
return payload["data"]
```

### 6.7 网关客户端（`gateway.py`，102 行）

`AgentHttpClient`（20-63）—— httpx 封装，自动注入该 agent 的 bearer token：

```python
class AgentHttpClient:
    def __init__(self, http, row, timeout):
        self._endpoint = row.endpoint.rstrip("/")  # ← 唯一耦合
        if row.auth_type == "bearer" and row.auth_token:
            self._headers["Authorization"] = f"Bearer {row.auth_token}"
    
    async def get_card(self) -> dict: ...        # GET /card
    async def health(self) -> bool: ...          # GET /health(timeout 3s,异常返 False)
    async def post(self, path, payload) -> Response: ...
    async def get(self, path) -> Response: ...
    def build_events_request(self, task_id, timeout): ...   # SSE 流
```

`map_downstream_error(exc)`（66-69）：`TimeoutException` → 408，其他 `HTTPError` → 502。

`insert_call_log(...)`（72-75）：建任务时落 call_logs。

**`record_terminal(...)`（78-101）是计费唯一挂点**：

```python
if state == TaskState.COMPLETED and row.charge:
    acc.balance -= row.charge
    CreditTxRow(delta=-row.charge, reason="charge", task_id=task_id)
```

任务成功完成才扣费；failed / canceled / input_required 不扣。

---

## 七、检索层

### 7.1 Embedding（`embedding.py`）

| 类 / 函数 | 行号 | 用途 |
|---|---|---|
| `Embedder` Protocol | 20-23 | 统一接口（`dim` + `async embed(texts)`） |
| `SentenceTransformerEmbedding` | 26-44 | 本地 bge-m3，**惰性加载** + **线程池执行**避免阻塞事件循环 |
| `HashEmbedding` | 47-65 | 字符 n-gram 哈希向量，**仅测试 / 无模型环境** |
| `build_embedder(settings)` | 68-71 | 工厂，按 `embedding_backend` 选择 |
| `card_embed_text(card)` | 74-84 | 拼接 `name + description + natural_capabilities + " ".join(capabilities)` |

**关键设计**：

- bge-m3 首次启动**不下载**，首跑时 `_load()` 触发
- `_encode` 在 `asyncio.to_thread` 中执行，CPU 密集型不阻塞事件循环

### 7.2 召回（`recall.py`）

```python
async def recall_top_k(session, vec, k) -> list[(AgentRow, distance)]:
    where = [status == ACTIVE, embedding IS NOT NULL]
    
    if dialect == "postgresql":
        # pgvector 余弦距离索引
        distance = AgentRow.embedding.cosine_distance(vec)
        return ORDER BY distance LIMIT k
    
    # fallback:全量取回 Python 计算(仅开发/测试规模)
    rows = SELECT WHERE *where
    scored = [(row, _cosine_distance(vec, row.embedding)) for row in rows]
    return sorted(scored)[:k]
```

`_cosine_distance`（14-18）—— 1 - 点积 / (||a|| * ||b||)。

---

## 八、治理层

### 8.1 限流（`governance.py:13-28`）

```python
class RateLimiter:
    """每 agent 滑动窗口限流(次/秒)。"""
    def __init__(self, rps): self._hits = defaultdict(deque)
    
    def allow(self, agent_id) -> bool:
        # 滑动窗口:pop 早于 now-1s 的 hit
        # 若 len(hits) >= rps → False
        # 否则 append now → True
```

**每 agent 独立分桶**——一个 agent 限流不会影响别的 agent。

### 8.2 熔断（`governance.py:31-64`）

```python
class CircuitBreaker:
    """每 agent 熔断:窗口内失败 ≥ 阈值 → 熔断 open_sec 秒,半开后放行一次。"""
    def pre_check(agent_id) -> bool:    # 放行 / 熔断
    def on_success(agent_id):          # 清失败 + 清熔断
    def on_failure(agent_id):          # 累计失败,达阈值触发熔断
```

> **Bug fix 历史**（`governance.py:2-4` 注释）：旧版 `_state` 是全局单值，一个 agent 熔断会挡掉所有 agent。当前实现所有状态按 `agent_id` 分桶。

`routes.py:243-247` 调用：

```python
def _governance_precheck(request, agent_id):
    if not request.app.state.rate_limiter.allow(agent_id):
        raise HTTPException(429, "该 agent 调用超出限流")
    if not request.app.state.breaker.pre_check(agent_id):
        raise HTTPException(503, "该 agent 处于熔断状态,请稍后再试")
```

`gateway.py:268-275` 反馈：

```python
except httpx.HTTPError: breaker.on_failure(agent_id); ...
if 5xx: breaker.on_failure(agent_id); ...
else: breaker.on_success(agent_id)
```

### 8.3 信誉聚合（`reputation.py`）

**`Reputation` 模型**（`packages/agentnet-core/src/agentnet_core/models.py:137-144`）：

```python
class Reputation(BaseModel):
    calls: int = 0
    success_rate: float = 0.0
    avg_latency_ms: float = 0.0
    rating: float | None = None       # 消费者评分均值
    canary_score: float | None = None # canary 通过率
```

**`load_reputations(session, agent_ids)`**（13-51）—— 从两张表实时聚合，**不存储**：

```
1. SELECT calls, AVG(success), AVG(latency_ms), AVG(rating)
   FROM call_logs WHERE agent_id IN (...) AND final_state IS NOT NULL
   GROUP BY agent_id

2. SELECT AVG(passed)
   FROM canary_logs WHERE agent_id IN (...)
   GROUP BY agent_id
   → 填入 canary_score
```

**为什么实时聚合不存储**：永远与 call_logs / canary_logs 一致，无对账成本。

**fallback**：`routes.py:79-85` —— 本地无调用记录时，回退到 `agents.remote_reputation`（联邦来源 registry 的快照）。

---

## 九、后台任务层（3 个 asyncio 任务）

由 `app.py:55-70` 在 lifespan 阶段启动。

### 9.1 共有的运行模式

```python
async def run(self):
    while not self._stopped.is_set():
        with contextlib.suppress(Exception):     # 任何异常永不影响主流程
            await self.tick()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stopped.wait(), timeout=self._interval)
```

### 9.2 Inspector 巡检（`inspector.py`，75 行）

**周期**：默认 60s。**目的**：检测 agent 健康 + 一致性，自动剔除 / 恢复。

```
每 tick:
  1. SELECT agent_id FROM agents WHERE provider NOT LIKE 'federated:%'
     (federated 行由 peer 自己治理,本地不巡检)
  2. 对每个 agent_id:
     health = await client.health()                       # GET /health
     card   = await client.get_card() (suppress exception)
     healthy = health AND card.agent_id == row.agent_id   # /card 身份必须一致
     if healthy:
       consecutive_health_failures = 0
       status = active
     else:
       consecutive_health_failures += 1
       if >= fail_threshold (默认 3):
         status = offline
```

**关键设计**：`/card` 一致性校验防"冒名 / 部署错位"——agent 服务重启后改了 endpoint 或 agent_id，Registry 立即发现并踢掉。

### 9.3 Canary 跑分（`canary.py`，119 行）

**周期**：默认 300s。**目的**：防能力欺诈（声称的能力必须能通过自出题）。

```
每 tick:
  1. SELECT agent_id, canary_cases FROM agents WHERE status=active
  2. 对每个有 canary_cases 的 agent:
     case = random.choice(cases)
     await client.post(/tasks, {message:{role:user, parts:[TextPart(case.query)]}})
     task = await _await_terminal(client, task_id)   # 轮询 GET /tasks/{id}
     answer = last_agent_text(task)
     passed = all(keyword in answer for keyword in case.expect)
     detail = "" if passed else f"回答缺少关键词: {missing}"
     INSERT canary_logs(agent_id, query, passed, detail, latency_ms)
```

**与巡检的区别**：

| | 巡检 | Canary |
|---|---|---|
| 频率 | 60s | 300s |
| 触发 | 自动(全 active agent) | 随机(provider 自出题) |
| 行为 | GET /health + /card | POST /tasks(实际跑业务) |
| 落表 | agents.status | canary_logs |
| 跳过 | federated 行 | (无 canary_cases 的 agent) |

### 9.4 Federation 联邦同步（`federation.py`，105 行）

**周期**：默认 60s。**目的**：把 peer registry 的 agent 同步为本地 `federated:` 行。

```
每 tick:
  对每个 peer:
    resp = await GET <peer.url>/v1/agents   (Authorization: Bearer peer.consumer_key)
    cards = AgentCard(**a) for a in resp.json()["data"]
    vecs = await embedder.embed([card_embed_text(c) for c in cards])  # 本地管线批量算
    
    seen = set()
    for data, card, vec in zip(agents, cards, vecs):
      seen.add(card.agent_id)
      row = await session.get(AgentRow, card.agent_id)
      if row is not None and not row.provider.startswith("federated:"):
        continue                              # 本地自有 agent 优先,不覆盖
      if row is None:
        row = AgentRow(agent_id=..., provider="federated:<name>")
      row.endpoint = f"{peer.url}/v1/agents/{card.agent_id}"   # ← 关键:指向 peer Gateway
      row.auth_token = peer.consumer_key                       # ← 持 peer 给的 consumer key
      row.canary_cases = []                                     # federated 不在本地跑 canary
      row.status = data["status"] if data["status"] in (active, offline) else active
      row.remote_reputation = data.get("reputation")            # 信誉快照
      row.embedding = vec
    
    DELETE FROM agents WHERE provider = "federated:<name>" AND agent_id NOT IN seen
      # peer 上消失的 federated 行同步删除(peer 不可达时不会走到这里)
```

**关键设计**：

- **本地优先**（75-76）：peer 同步不会覆盖本地自有 agent
- **federated 行 endpoint 指向 peer 的 Gateway**（86）：调用经"本地 registry → peer registry → agent"链式代理，**agent credential 始终由 peer 持有**
- **巡检 / canary 不作用于 federated 行**（`inspector.py:54`、`canary.py` 未读 federated）：由 peer 自己治理
- **peer 不可达保留陈旧数据**（`federation.py:53` `suppress(Exception)`）：不影响主流程

### 9.5 后台任务启动序列（`app.py:55-70`）

```python
inspector = Inspector(sm, http, settings.inspect_interval_sec, settings.inspect_fail_threshold)
inspect_task = asyncio.create_task(inspector.run()) if settings.inspect_enabled else None

canary = CanaryRunner(sm, http, settings.canary_interval_sec, settings.canary_timeout_sec)
canary_task = asyncio.create_task(canary.run()) if settings.canary_enabled else None

federation = FederationSync(sm, http, embedder, settings.peers, settings.federation_sync_interval_sec)
federation_task = asyncio.create_task(federation.run()) if settings.federation_enabled and settings.peers else None
```

lifespan 关闭时（`app.py:74-81`）调 `stop()` + `task.cancel()` 优雅退出。

---

## 十、横向数据流

```
admin 签 key        provider 发 AgentCard         consumer 提问
    │                     │                            │
    ▼                     ▼                            ▼
POST /v1/keys        POST /v1/agents            POST /v1/agents/{id}/tasks
    │                     │                            │
    ▼                     ▼                            │
generate_key()       握手 GET /card                   │
    │                     │                            │
    ▼                     ▼                            │
sha256 入库          embedding 算向量                  │
    │                     │                            │
    ▼                     ▼                            │
返明文(一次性)       upsert agents 表                 │
    │                     │                            │
    ▼                     ▼                            │
admin 转交给         status=active                   │
provider /                                          ▼
consumer                                          rate_limit_rps.allow(agent_id) → 429?
                                                   breaker.pre_check(agent_id) → 503?
                                                   CreditAccountRow.balance < charge → 402?
                                                        │
                                                        ▼ all OK
                                                   insert_call_log(task_id, agent_id, consumer)
                                                        │
                                                        ▼
                                                   proxy POST <endpoint>/tasks
                                                        │
                                                        ▼
                                                   代理 → agent → 返回 Task JSON
                                                        │
                                                        ▼
                                                   consumer 调 GET .../tasks/{tid}/events
                                                        │
                                                        ▼ SSE
                                                   proxy GET <endpoint>/tasks/{tid}/events
                                                        │
                                                        ▼ 旁路
                                                   record_terminal(task_id, state)
                                                        │
                                                        ▼ completed?
                                                   CreditAccountRow.balance -= charge
                                                   CreditTxRow(reason=charge)
```

---

## 十一、关键设计决策与边界

| 决策 | 体现位置 |
|---|---|
| **Registry 永不拉取 agent 代码** | `grep agentnet_sdk` 在 registry 包 → 0 命中；`grep subprocess\|importlib\|exec` → 0 命中 |
| **Key 明文仅签发时返回一次** | `routes.py:105` 注释 + `api_keys.key_hash` 仅存 sha256 |
| **消费者看不到 agent 真实地址** | `_mask_card()`（`routes.py:53-67`） |
| **Agent 自填信誉不可信** | `Reputation` 字段**不**出现在 AgentCard（`models.py:138`）；Registry 实时聚合 |
| **federated 行的 credential 始终由 peer 持有** | `federation.py:86-88`（endpoint 指向 peer Gateway） |
| **本地自有 agent 优先** | `federation.py:75-76` |
| **Canary 在 Gateway 之外** | `canary.py` 直接 POST /tasks，不进 call_logs、不扣消费者积分 |
| **巡检用 `/card` 一致性防冒名** | `inspector.py:43-44` 比对 agent_id |
| **熔断每 agent 分桶** | `governance.py:38-39`（注释说明旧版 bug） |
| **Embedding 惰性加载 + 线程池** | `embedding.py:42-44`，启动不下载模型、首跑在后台线程 |
| **跨方言 VectorType** | `db.py:25-35`（pgvector / JSON 自动切换） |
| **限流/熔断仅作用于数据面** | `routes.py:243-247` 只在 Gateway 调用前 |
| **后台任务永不抛异常影响主流程** | 三者共有的 `contextlib.suppress(Exception)` 包裹 |
| **联邦行不参与本地巡检/canary** | `inspector.py:54` WHERE 子句 + `canary.py` 不读 federated |
| **completed 才扣费** | `gateway.py:95-101` |

### 已知边界

1. **没有 key 吊销接口**——只能删 / 改 env 重启
2. **没有"暂停 / deactive"接口**——只能等巡检自动 offline
3. **重注册会强行把 offline 拉回 active**——巡检是唯一的状态收敛源
4. **federated 行的状态完全由 peer 决定**——本地无法自证
5. **Registry 多副本部署会重复执行后台任务**——需要选主 / 分布式锁
6. **计费余额预检并发下可透支**——无行锁 / 预授权冻结

---

## 十二、开发与调试速查

### 12.1 跑测试

```bash
uv run pytest packages/agentnet-registry             # SQLite + hash embedding,无需外部服务
uv run pytest packages/agentnet-registry -k requires_db  # PostgreSQL + pgvector(需 docker compose up)
```

### 12.2 本地启动

```bash
# 开发模式(SQLite)
AGENTNET_DATABASE_URL=sqlite+aiosqlite:///./agentnet.db uv run agentnet registry

# 生产模式(Postgres)
docker compose up -d
AGENTNET_ADMIN_KEY=<随机字符串> uv run agentnet registry
```

### 12.3 调试某个后台任务

后台任务的异常被 `contextlib.suppress(Exception)` 吞掉。临时调试：

```python
# 在 inspector.py:30 改:
async def run(self):
    while not self._stopped.is_set():
        await self.tick()                          # ← 去掉 suppress
        await asyncio.wait_for(self._stopped.wait(), timeout=self._interval)
```

### 12.4 联邦本地双实例

```bash
# Terminal 1
AGENTNET_PORT=9000 AGENTNET_ADMIN_KEY=admin-a uv run agentnet registry

# Terminal 2
AGENTNET_PORT=9001 AGENTNET_ADMIN_KEY=admin-b \
AGENTNET_PEERS='[{"url":"http://localhost:9000","consumer_key":"<admin-a签的>","name":"p1"}]' \
uv run agentnet registry
```

---

## 十三、相关文档

- 协议与角色速查：[QA.md](QA.md)
- 架构图与全链路时序：[ARCHITECTURE.md](ARCHITECTURE.md)
- Provider 接入手册：[PROVIDER_REGISTRATION.md](PROVIDER_REGISTRATION.md)
- 创作者文档集：[for_creator/](for_creator/)
- 模块交接与下一步：[HANDOFF.md](HANDOFF.md)