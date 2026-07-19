# AgentNet Enterprise Gateway

企业内 AI Agent 资产网关与编排引擎 MVP 实现。对齐 `docs/DEV_DOCUMENTS.md` 全部规格。

## 已交付能力

| 规格条款 | 实现位置 | 状态 |
|---|---|---|
| §1 单轮对话、Fail-Fast、非流式 JSON | 全局 envelope 协议 | ✅ |
| §2.1 Agent 注册 Manifest | `app/schemas/agent.py`, `app/api/agents.py` | ✅ |
| §2.2 网关错误码 200/400/404/408/429/502/503 | `app/core/codes.py` + 全局异常处理 | ✅ |
| §2.3 X-AgentNet-Token / X-Caller-Dept 注入 | `app/services/expert_client.py:140-150` | ✅ |
| §3.1 fast_route (max_tokens=10, temperature=0) | `app/services/fast_route.py` | ✅ |
| §3.2 deep_plan Tool Use (tool_choice=required) | `app/services/deep_plan.py` | ✅ |
| §3.3 2K BPE token 强截断 + {{steps.N.output}} 占位符 | `app/core/token_clip.py`, `app/services/dispatcher.py:_render_sub_query` | ✅ |
| §3.4 Fail-Fast 任一节点失败 → 中断 | `app/services/dispatcher.py` `dispatch()` 内层 break | ✅ |
| §4 4 张表 + 8 索引 + 状态机 | `app/db/models.py` | ✅ |
| §5.1 单 Agent 限流 + 10s/5 次熔断 60s | `app/core/ratelimit.py`, `app/core/circuit_breaker.py` | ✅ |
| §5.2 分层健康检查 (核心30s/边缘5m) + 连续 3 失败 offline | `app/services/health_service.py` | ✅ |
| §6 跨部门联动对数 + 复用广度 + 饱和度 | `app/services/metrics_service.py`, `app/api/metrics.py` | ✅ |
| §7-Task1~4 全部交付 | 见 `tests/test_e2e.py` 12 项断言 | ✅ |

## 目录结构

```
AgentNet/
├── app/
│   ├── main.py                  FastAPI 入口 + lifespan
│   ├── config.py                pydantic-settings 配置
│   ├── deps.py                  FastAPI Depends 工厂
│   ├── schemas/                 Pydantic v2 数据契约
│   │   ├── agent.py             Manifest 入参/出参
│   │   ├── session.py
│   │   ├── plan.py              状态机三态
│   │   ├── call_log.py
│   │   ├── metrics.py
│   │   └── response.py          网关统一 Envelope
│   ├── core/                    网关核心 (无 IO)
│   │   ├── codes.py             错误码 + GatewayError
│   │   ├── request_id.py        contextvars 透传
│   │   ├── token_clip.py        BPE 2K 硬截断
│   │   ├── ratelimit.py         单 Agent 滑动窗口限流
│   │   └── circuit_breaker.py   熔断器
│   ├── db/
│   │   ├── models.py            SQLAlchemy ORM (4 表 + 8 索引)
│   │   └── session.py           异步 engine / sessionmaker
│   ├── llm/
│   │   └── client.py            OpenAI 兼容异步 LLM 客户端
│   ├── services/
│   │   ├── agent_service.py
│   │   ├── session_service.py
│   │   ├── plan_service.py
│   │   ├── call_log_service.py
│   │   ├── expert_client.py     下游 Expert Agent HTTP 客户端
│   │   ├── fast_route.py        一层意图裁决
│   │   ├── deep_plan.py         二层 Tool Use 编排器
│   │   ├── dispatcher.py        DAG 执行器 (Fail-Fast)
│   │   ├── health_service.py    分层健康探测
│   │   └── metrics_service.py
│   ├── api/                     FastAPI routers
│   └── scheduler.py             APScheduler (health tick)
├── scripts/seed.py              建表 + 默认 Agent / Session
├── tests/
│   ├── test_e2e.py              12 项端到端冒烟
│   ├── smoke_dispatcher.py      topo_layers 单元
│   └── smoke_governance.py      限流 + 熔断单元
├── docs/DEV_DOCUMENTS.md
├── docker-compose.yml           PostgreSQL 16 容器
├── requirements.txt
├── .env.example
└── README.md
```

## 快速启动

### 1. 起 PostgreSQL

```bash
docker compose up -d postgres
# 等待 healthcheck 后
python -m scripts.seed    # 建表 + 灌入默认 3 个示例 Agent
python -m scripts.seed --reset   # 重置
```

### 2. 配置 `.env`

```bash
cp .env.example .env
# 关键项:
#   AGENTNET_DATABASE_URL=postgresql+asyncpg://agentnet:agentnet@localhost:5432/agentnet
#   AGENTNET_LLM_BASE_URL=https://api.deepseek.com/v1
#   AGENTNET_LLM_API_KEY=sk-xxx
#   AGENTNET_LLM_MODEL=deepseek-chat
```

### 3. 启动网关

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
# 访问 http://localhost:8000/docs 看 OpenAPI
```

### 4. 注册一个专家 Agent

```bash
curl -X POST http://localhost:8000/agents \
  -H 'Content-Type: application/json' \
  -d '{
    "agent_id": "corp.demo.echo",
    "name": "回显专家",
    "owner_department": "测试组",
    "description": "回声专家。把 sub_query 原样加 [ECHO] 前缀。",
    "endpoint": { "url": "http://127.0.0.1:9001/api/v1/chat", "x_agentnet_token": "st_test" },
    "sla": { "timeout_ms": 5000, "max_retry": 0 }
  }'
```

### 5. 创建 Session 并发起一次 Chat

```bash
curl -X POST http://localhost:8000/sessions \
  -H 'Content-Type: application/json' \
  -d '{"session_id": "demo-1", "user_id": "u-alice", "caller_dept": "研发中心-后端组"}'

curl -X POST http://localhost:8000/chat \
  -H 'Content-Type: application/json' \
  -d '{"session_id": "demo-1", "query": "你好"}'
```

返回:

```json
{
  "request_id": "req-9a8b7c6d5e4f",
  "code": 200,
  "message": "Success",
  "data": { "content": "...", "plan_id": 1, "agent_id": "corp.demo.echo" }
}
```

## 测试

```bash
# 12 项端到端冒烟 (mock LLM + mock Expert; 不依赖 DB)
python tests/test_e2e.py

# 单元
python tests/smoke_dispatcher.py
python tests/smoke_governance.py
```

## 关键设计决策

1. **JSONB vs JSON**: `app/db/models.py:_AdaptiveJSON` 让 PG 上用真正的 JSONB；开发用 SQLite 时自动降级为 JSON，单元测试友好。
2. **响应编码**: `app/main.py:UTF8JSONResponse` 覆盖默认 JSONResponse，强制 `ensure_ascii=False`，保证中文 / 多字节不被转义。
3. **审计异步写**: `app/services/call_log_service.py:fire_and_forget_record` 通过 `asyncio.create_task` 不阻塞主流程，失败仅记日志不影响响应。
4. **限流 / 熔断**: 进程内 in-memory。多进程部署需要换 Redis；MVP / 内网单机足够。
5. **健康检查分层**: 上次探测时间戳用 `app/services/health_service.py:HealthService._last_probed_at` in-memory dict 维护，避免给规格书之外的 schema 加字段。
6. **DAG 拓扑**: Kahn 算法，按层并发 (`asyncio.gather`)、层间串行，同层任一失败即中断后续层（SPEC §3.4）。

## 已知未实现

- 流式响应 / SSE (SPEC §1 明确 MVP 不做)
- 真实 KMS / Vault 存储 auth_token (SPEC §4 标 v1.1 必须迁移；MVP 暂明文)
- 多进程网关下的共享 ratelimit / circuit_breaker (MVP 限制为单进程)
- Alembic 数据库迁移 (MVP 走 `Base.metadata.create_all`)

## 配置项速查

`AGENTNET_*` 前缀的环境变量，参考 `.env.example`。核心可调:

| 变量 | 默认 | 用途 |
|---|---|---|
| `AGENTNET_STEP_OUTPUT_TOKEN_LIMIT` | 2048 | 上游输出注入时 token 上限 |
| `AGENTNET_AGENT_RPS_LIMIT` | 20 | 单 Agent 每秒请求上限 |
| `AGENTNET_BREAKER_WINDOW_SEC` | 10 | 熔断滑动窗口 |
| `AGENTNET_BREAKER_FAIL_THRESHOLD` | 5 | 窗口内失败次数 |
| `AGENTNET_BREAKER_OPEN_SEC` | 60 | 熔断开启秒数 |
| `AGENTNET_HEALTH_CORE_INTERVAL_SEC` | 30 | 核心 Agent 心跳间隔 |
| `AGENTNET_HEALTH_EDGE_INTERVAL_SEC` | 300 | 边缘 Agent 心跳间隔 |
| `AGENTNET_HEALTH_PROBE_TIMEOUT_MS` | 2000 | 单次探针超时 |
| `AGENTNET_HEALTH_FAIL_THRESHOLD` | 3 | 连续失败 N 次置 offline |

## License

Internal MVP. Please follow corporate policy.
