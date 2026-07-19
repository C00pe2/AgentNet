# 🚀 AgentNet Enterprise - 生产级技术规格书 (v1.0-V3 Final)

## 1. 系统边界与 MVP 核心约束 (System Scope & Constraints)

AgentNet 是企业内部的 **AI Agent 资产网关与运行时编排引擎 (Gateway & Orchestration Engine)**。为确保 MVP 阶段快速交付，系统设计遵循以下绝对约束：

1. **网络与环境：** 纯内网环境部署。网络互信，暂不引入复杂的 RBAC 与 OAuth2 认证机制。
2. **会话模型：** **仅支持单轮对话**。Planner 大脑不感知历史上下文，`session_id` 仅作为全链路审计的外键。
3. **失败模型：** 采取 **Fail-Fast（强一致性破坏）** 策略。DAG 任务图中任意一个 Expert Agent 节点失败，整个 Plan 立即宣告失败。
4. **响应模式：** **全量采用非流式 JSON 响应**。暂不支持 Stream/SSE，降低网关 hold 住长任务的连接挂起风险。

---

## 2. 统一通信契约与错误码约定 (Protocol & Specs)

### 2.1 专家 Agent 准入协议 (Manifest Schema)

业务方注册 Agent 时需提交的元数据规格：

```json
{
  "agent_id": "corp.tech.security.code-auditor",
  "name": "Java底层代码安全审计专家",
  "owner_department": "技术保障部-安全组",
  "description": "专门用于审查Java、Spring框架底层的反序列化和SQL注入漏洞。输入必须包含Java源码片段。",
  "endpoint": {
    "url": "http://10.24.56.12:8080/api/v1/chat",
    "x_agentnet_token": "st_abc123789xyz" 
  },
  "sla": {
    "timeout_ms": 15000,
    "max_retry": 1
  }
}

```

### 2.2 网关统一错误码与响应结构 (Unified Response & Error Codes)

网关面向前端及内部调度层，统一采用标准结构体。客户端或调度层一律根据 `code` 进行强类型分支处理。

* **标准响应结构：**
```json
{
  "request_id": "req-9a8b7c6d-5e4f",
  "code": 200, 
  "message": "Success",
  "data": {
    "content": "经过审计，未发现高危反序列化风险。"
  }
}

```


* **网关标准错误码定义：**
* `200`: `SUCCESS` — 成功。
* `400`: `BAD_REQUEST` — 用户输入或请求参数解析错误。
* `404`: `NO_AGENT_MATCHED` — 二层路由可用 tools 列表为空、或全员 offline，无法匹配到任何专家 Agent 完成当前任务。
* `408`: `AGENT_TIMEOUT` — 下游专家 Agent 响应超时。
* `429`: `ROUTE_LIMIT_EXCEEDED` — 触发了网关或单 Agent 的过载保护限流。
* `502`: `AGENT_5XX_ERROR` — 下游专家 Agent 服务崩溃或返回非 200 状态码。
* `503`: `PLAN_ORCHESTRATION_FAILED` — 平台 Planner 规划失败、Tool Use 格式错误或断路器触发。


* **下游 Agent 错误码归一：** Expert Agent 自身返回 `4xx` 视为网关 `400 BAD_REQUEST`；`5xx` 与非 200 一律视为 `502 AGENT_5XX_ERROR`；响应超时一律 `408 AGENT_TIMEOUT`。

### 2.3 认证与调用方身份最小约定 (Identity Delivery)

鉴于内网互信环境，网关层不对外暴露复杂的 Token 签发。

* **网关对下游专家：** 网关请求 Expert Agent 时，必须在 Header 中携带注册时指定的 `X-AgentNet-Token` 进行静态令牌验签。
* **透传身份识别：** 网关在向下游分发请求时，必须在 HTTP Header 中强制注入 `X-Caller-Dept`（从最初的 Session 中提取），供专家 Agent 侧自行做内部审计或日志打标。

---

## 3. 双层大模型路由与强管控编排引擎 (Routing & Orchestration)

### 3.1 一层路由：轻量级意图裁决（Fast Route）

* **模型配置：** `max_tokens=10`, `temperature=0`。
* **逻辑：** 快速分类。判断能直达某专家还是定义为复杂任务。若大模型输出无法匹配现有 `agent_id` 或返回异常，直接降级分类为 `COMPLEX` 唤醒二层路由。
* **System Prompt 规范：**
```text
你是一个高并发的请求分发器。请阅读以下可用的专家Agent列表及核心能力：
{SHORT_AGENT_LIST_JSON}

请分析用户的输入，并在下面的选项中做出单选，你【必须且只能】从选项中挑选一个作为输出，不要包含任何解释或标点符号：
1. 如果用户意图非常单一，且现有某个专家Agent能够【完美独立搞定】，请直接输出该Agent的 "agent_id"。
2. 如果用户意图复杂、需要多步拆解、或者需要多个专家Agent协同，请直接输出 "COMPLEX"。
3. 如果所有专家Agent都不匹配，请直接输出 "UNKNOWN"。
```

### 3.2 二层路由：基于 Tool Use 的高级编排器（Deep Planner）

网关将所有已注册的专家 Agent 的描述动态组装为大模型的 `tools` 列表，强制开启结构化 Tool 调用以生成 DAG。

### 3.3 Dispatcher 上下文注入与截断策略 (Context Clipping)

网关层在解析出 DAG 依赖，准备将 Step N-1 的输出注入到 Step N 的 `sub_query` 时，必须执行强管控：

* **Token 强截断：** 注入数据前，网关层必须使用分词器进行计算，上游输出的 `data.content` 限制最大 `2048 tokens`（BPE 分词后约 1000 汉字字符，由网关分词器精确计算，不使用固定字符比例换算）。
* **超限截断语义：** 超过 2048 tokens 的部分直接进行硬截断，并在尾部追加 `[... 内容因超出网关 2K Token 限制已被截断 ...]`。网关不在 MVP 阶段提供长文本持久化拉取接口，强迫上游专家 Agent 精简输出。

### 3.4 Plan 执行失败处置策略 (Failure Model)

遵循 **Fail-Fast** 绝对原则，确保状态机和逻辑的极简：

* **任一节点失败 $\rightarrow$ 整 Plan 失败：** 当 DAG 链条中任意一个专家 Agent 在历经 `max_retry` 重试后仍然返回非 200、超时（`408`）或崩溃（`502`）时，网关**立即中断**后续所有节点的异步或串行调度。
* **熔断与清理：** 终止后续执行后，网关不需要回滚已完成节点在第三方服务中的数据状态，立即向用户响应 `503 PLAN_ORCHESTRATION_FAILED`。

---

## 4. 生产级数据模型与状态机 (Database & State Machine)

全面升级为包含 `agent_sessions`、`plan_executions`、`agent_call_logs` 的三层联动追溯模型。

```sql
-- 1. 专家Agent注册主表
CREATE TABLE agents (
    agent_id VARCHAR(100) PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    owner_dept VARCHAR(100) NOT NULL,
    description TEXT NOT NULL,
    endpoint_url VARCHAR(255) NOT NULL,
    auth_token VARCHAR(255) NOT NULL, -- MVP 阶段为方便落地暂以明文存储；v1.1 必须迁移至 KMS / Vault 加密
    timeout_ms INT DEFAULT 15000,
    max_retry INT DEFAULT 1,
    status VARCHAR(20) DEFAULT 'active', -- active, offline
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 2. 全局全局会话表（锚定调用根源）
CREATE TABLE agent_sessions (
    session_id VARCHAR(100) PRIMARY KEY,
    user_id VARCHAR(50) NOT NULL,
    caller_dept VARCHAR(100) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 3. 编排计划执行表（次级聚合根，定义Plan状态机）
CREATE TABLE plan_executions (
    plan_id BIGSERIAL PRIMARY KEY,
    session_id VARCHAR(100) REFERENCES agent_sessions(session_id) ON DELETE RESTRICT,
    raw_query TEXT NOT NULL,
    execution_graph JSONB NOT NULL, -- 存储大模型返回的原始 tool_calls DAG 拓扑
    status VARCHAR(20) DEFAULT 'RUNNING', -- 状态机三态：RUNNING, SUCCEEDED, FAILED
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 4. 节点调用明细审计表（支持全链路追踪与报文快照，统一命名一致性）
CREATE TABLE agent_call_logs (
    log_id BIGSERIAL PRIMARY KEY,
    plan_id BIGINT REFERENCES plan_executions(plan_id) ON DELETE SET NULL,
    session_id VARCHAR(100) REFERENCES agent_sessions(session_id) ON DELETE RESTRICT,
    target_agent_id VARCHAR(100) REFERENCES agents(agent_id),
    step_id INT NOT NULL,             -- DAG图中的步骤序号
    request_snapshot JSONB NOT NULL,  -- 网关实际发出的请求体快照
    response_snapshot JSONB,          -- 专家Agent实际返回的响应体快照
    code INT NOT NULL,                -- 对应网关标准错误码
    latency_ms INT NOT NULL,          -- 毫秒级耗时审计
    status VARCHAR(20) NOT NULL,      -- SUCCESS, FAILED
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_logs_route ON agent_call_logs(target_agent_id);
CREATE INDEX idx_logs_session ON agent_call_logs(session_id);
CREATE INDEX idx_logs_plan ON agent_call_logs(plan_id);
CREATE INDEX idx_logs_created ON agent_call_logs(created_at);
CREATE INDEX idx_logs_agent_time ON agent_call_logs(target_agent_id, created_at);
CREATE INDEX idx_plans_session ON plan_executions(session_id);
CREATE INDEX idx_agents_status ON agents(status);
CREATE INDEX idx_sessions_dept ON agent_sessions(caller_dept);

```

### 4.1 Plan 核心状态机演进图

* **`RUNNING` $\rightarrow$ `SUCCEEDED**`：DAG 图中所有编排节点全部顺利返回 `200` 且数据流转完成。
* **`RUNNING` $\rightarrow$ `FAILED**`：任意节点触发 `408`/`502` 且重试耗尽，此 Plan 状态立刻转为 `FAILED`，网关向 `agent_call_logs` 写入失败快照，中断后续流程。

---

## 5. 网关治理与动态健康检查 (Governance & Resilience)

### 5.1 断路器与单 Agent 限流保护 (Circuit Breaker & Rate Limit)

* **网关层单节点限流：** Dispatcher 为每个 `agent_id` 维护一个内存计数器，限制单个专家 Agent 的企业内部最大 RPS 为 `20`。超限请求直接在网关层拦截并抛出 `429 ROUTE_LIMIT_EXCEEDED`。
* **主动熔断：** 针对单一 `agent_id`，若在 10 秒内连续出现 5 次 `408 (超时)` 或 `502 (崩溃)`，网关主动将该 Agent 熔断 60 秒。期间所有请求在网关直接返回 `502`，保护内网公共链路。

### 5.2 动态分层健康检查机制 (Adaptive Health Check)

为防止全量 30s 轮询变成内网 DDOS，采用分层频率设计，并明确定义“什么是病”：

* **分层检查频率：**
* **核心 Agent（近 1 小时内有真实请求）：** 每 30 秒发送一次轻量 `/health` 心跳。
* **边缘长尾 Agent（超过 24 小时无请求）：** 降级至每 5 分钟发送一次心跳。


* **失败判定与降级标准：**
* **判定标准：** 凡是满足【HTTP 状态码非 200】或【网络连接被拒绝】或【单次 Ping 延迟 $>$ 2000ms】均判定为一次探测失败。
* **下线隔离：** 一旦某专家 Agent 连续 3 次探测失败，网关自动将其在内存及数据库中的状态更新为 `offline`。在二层路由组装 `tools` 列表时，**自动剔除所有 offline 的 Agent**，避免大模型把任务规划给“死节点”。



---

## 6. 企业级复用度量与网络对数 (Metrics)

MVP 阶段严禁上线无数据支持的人月成本自嗨公式，看板只展示完全可辩护的相对值指标：

1. **跨部门网络联动对数 (Cross-Dept Linking Pairs)：**

$$\text{联动对数} = \text{Count}(\text{Distinct}(\text{agent\_sessions.caller\_dept} \rightarrow \text{agents.owner\_dept}))$$


2. **公共资产复用深度 (Reusability Depth)：**

$$\text{复用广度} = \text{该 Agent 关联的独立跨部门调用方数量 (Unique } \text{caller\_dept})$$


$$\text{调用跨度饱和度} = \frac{\text{非本部门调用次数}}{\text{全局总调用次数}} \times 100\%$$



---

## 7. Coding Agent 模块化交付路线图 (Todo List)

名称一致性与逻辑闭环已修正，请按以下模块严格编写：

* [ ] **Task 1: 基础设施与生产级三层存储模型**
* 使用 FastAPI (Python) 或 Go 搭建骨架。
* 编写 `agents`, `agent_sessions`, `plan_executions`, `agent_call_logs` 表的 SQLAlchemy Models / CRUD。


* [ ] **Task 2: 分层健康检查与动态隔离服务**
* 实现基于定时的后台双层轮询任务（核心 30s / 边缘 5m）。
* 实现“连续 3 次探测失败（含延迟 $>$ 2s）即标记 `offline`”的摘除逻辑。


* [ ] **Task 3: Tool Use 结构化路由与网关状态机**
* 编写 `fast_route` 判定服务，处理 `COMPLEX` / `UNKNOWN` 分支。
* 编写 `deep_plan`，动态把 `status='active'` 的 Agent 转化为大模型的 tools 列表，强制开启 Tool 调用。
* 实现 Dispatcher 调度，加入 `{{steps.N.output}}` 占位符解析、**2K Tokens 长度硬截断**以及**任一节点失败即整 Plan 变更为 FAILED** 并强行中断的 Fail-Fast 逻辑。


* [ ] **Task 4: 网关强契约与审计度量**
* 封装全局网关异常，确保所有报错返回包含 `request_id` 的标准 JSON 错误响应。
* 编写 `agent_call_logs` 异步写入，记录原始 Request/Response 的 JSONB 快照。
* 实现复用广度与跨部门联动对数的聚合 API。