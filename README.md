# 🚀 AgentNet Enterprise (MVP) - 核心技术规格与开发文档

## 1. 项目概述

**AgentNet** 是公司内部的 **Agent 资产网关与共享路由生态**。它旨在解决公司内部各业务团队独立开发的“专家级 Agent”无法被跨部门复用、无法原子化组合的痛点。
通过统一的**接入协议**和**智能路由大脑**，AgentNet 将全公司的 Agent 资产盘活，实现“一个主入口，按需路由至专家 Agent，多 Agent 协同解决复杂任务”的目标。

---

## 2. 核心架构设计 (MVP 阶段)

MVP 阶段采用**轻量级网关代理模式**，AgentNet 本身不托管任何算力或代码，只做**状态维护、意图路由、上下文传递和数据审计**。

```
[ 用户 / 客户端 ]
       │ (统一提问)
       ▼
┌────────────────────────────────────────────────────────┐
│                  AgentNet 主路由中心                     │
│  1. 接收请求 -> 2. 静态/动态路由 -> 3. 并发分发与裁剪     │
└──────────────────────────┬─────────────────────────────┘
                           │
         ┌─────────────────┼─────────────────┐
         ▼ (符合统一协议)    ▼                 ▼
  ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
  │ 团队A: 审计  │   │ 团队B: 文档  │   │ 团队C: 财务  │
  │ Expert Agent │   │ Expert Agent │   │ Expert Agent │
  └──────────────┘   └──────────────┘   └──────────────┘

```

---

## 3. 阶段一：定义统一的“专家名片”协议 (Protocol)

每个专家 Agent 必须在 AgentNet 注册一张“名片”。推荐使用 **JSON / YAML Schema** 描述。

### 3.1 专家 Agent 注册元数据定义 (Manifest)

```json
{
  "agent_id": "corp.tech.security.code-auditor",
  "name": "Java底层代码安全审计专家",
  "version": "v1.0.2",
  "owner_department": "技术保障部-安全组",
  "contact_email": "security-tech@corp.com",
  "description": "专门用于审查Java、Spring框架底层的反序列化、SQL注入及权限绕过漏洞。不擅长前端代码审计，不擅长编写业务代码。",
  "capabilities": [
    "Java代码审计",
    "漏洞扫描报告分析",
    "安全合规检查"
  ],
  "endpoint": {
    "url": "http://10.24.56.12:8080/api/v1/chat",
    "auth_type": "BearerToken",
    "token_secret_key": "SEC_AGENT_TOKEN_ENV_VAR" 
  },
  "health_check_url": "http://10.24.56.12:8080/health",
  "status": "active"
}

```

### 3.2 统一的专家 Agent 通信接口规范

所有接入的专家 Agent，其 `endpoint.url` 必须接受以下格式的 `POST` 请求，并返回统一的 Stream 或 JSON。

* **Request Body:**
```json
{
  "session_id": "global-session-xyz-123",
  "task_id": "sub-task-001",
  "query": "请帮我分析以下这段Java代码是否存在SSR漏洞：\n[Code Block]",
  "context_data": {
    "related_files": []
  }
}

```


* **Response Body (JSON模式示例):**
```json
{
  "status": "success",
  "data": {
    "content": "经过审计，发现该代码存在明文拼接URL隐患，建议使用..."
  }
}

```



---

## 4. 阶段二：智能路由大脑设计 (Planner & Router)

路由大脑采用“快慢结合”的双层路由机制，防止全量请求调用大模型导致的延迟与 Token 浪费。接入大模型进行逻辑意图识别。为了防止复杂任务拖慢所有请求，路由大脑采用 **“轻量快速分类”** 与 **“高级深度规划”** 的双层大模型路由机制。

```
                    [ 用户输入 Query ]
                            │
                            ▼
    ┌──────────────────────────────────────────────┐
    │     一层路由：轻量大模型 (如 Qwen-7B / 8B)      │
    │  - 目标：快速判断“直达专家”或“判定为复杂任务”     │
    └───────────────────────┬──────────────────────┘
                            │
            ┌───────────────┴───────────────┐
      (单意图/直达)                    (复杂任务/多步)
            │                               │
            ▼                               ▼
    ┌───────────────┐             ┌───────────────────────────────┐
    │  直接分发执行  │             │ 二层路由：主大模型 (如 72B 级别) │
    │ (Direct Route)│             │  - 目标：进行复杂的 Task Plan  │
    └───────────────┘             └───────────────┬───────────────┘
                                                  │
                                                  ▼
                                          [ 生成依赖链并串/并行分发 ]

```

### 4.1 一层路由：轻量级意图裁决（Fast Route）

* **选用模型：** 内网部署的轻量级开源大模型（如 `Qwen-2.5-7B/8B-Instruct`、`Llama-3-8B`），追求极低的首字延迟（TTFT）和高并发。
* **核心逻辑：** 仅让它做一件事情 —— **单选或分类**。判断用户的 Query 能不能由现有的某一个专家 Agent 独立直接搞定。如果能，直接输出该 Agent ID；如果觉得复杂或者需要多个工具配合，直接输出 `COMPLEX`。
* **System Prompt 规范：**
```text
你是一个高并发的请求分发器。请阅读以下可用的专家Agent列表及核心能力：
{SHORT_AGENT_LIST_JSON}

请分析用户的输入，并在下面的选项中做出单选，你【必须且只能】从选项中挑选一个作为输出，不要包含任何解释或标点符号：
1. 如果用户意图非常单一，且现有某个专家Agent能够【完美独立搞定】，请直接输出该Agent的 "agent_id"。
2. 如果用户意图复杂、需要多步拆解、或者需要多个专家Agent协同，请直接输出 "COMPLEX"。
3. 如果所有专家Agent都不匹配，请直接输出 "UNKNOWN"。

```


* **Coding Agent 实现要点：**
* 将大模型的 `max_tokens` 严格限制在 `10` 以内，`temperature` 设为 `0`（确保输出确定性）。
* 如果一层路由返回了具体的 `agent_id`，直接进入分发阶段，**耗时可控制在几百毫秒内**。



### 4.2 二层路由：高级任务规划器（Deep Planner）

* **触发条件：** 当一层路由明确返回 `"COMPLEX"` 时唤醒。
* **选用模型：** 内网能力最强的旗舰大模型（如 `Qwen-2.5-72B-Instruct` 或公司闭源大模型主力版本）。
* **核心逻辑：** 充分发挥大模型的推理能力，将复杂任务拆解为 DAG（有向无环图）形态的依赖链。
* **System Prompt 规范：**
```text
你是一个企业级复杂任务调度大脑。下面是目前内网可用的专家Agent列表及能力描述：
{FULL_AGENT_LIST_JSON}

请根据用户的复杂输入进行任务拆解（Plan）。你需要输出一个严格符合以下 JSON 格式的依赖链，不要包含任何 Markdown 标记或多余的文本：
{
  "plan": [
    {
      "step": 1,
      "agent_id": "corp.tech.security.code-auditor",
      "sub_query": "提取这段代码中的数据库连接部分并检查安全漏洞",
      "depends_on": []
    },
    {
      "step": 2,
      "agent_id": "corp.tech.doc.writer",
      "sub_query": "根据step 1返回的漏洞结果，编写一份符合公司内网规范的修复报告",
      "depends_on": [1]
    }
  ]
}

```

### 4.3 **分发与执行（Dispatcher）
* 根据路由结果，并发或串行请求专家 Agent。
* **上下文裁剪：** 只把大模型 Planner 拆解后的子任务 `query` 发给专家，不发送无关的历史上下文。



---

## 5. 阶段三：数据存储与复用看板 (Analytics)

为了向管理层证明项目的“资产盘活”价值，系统必须记录每次调用的复用链路。

### 5.1 核心数据库表设计 (PostgreSQL 示例)

```sql
-- 1. 专家Agent注册表
CREATE TABLE agents (
    agent_id VARCHAR(100) PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    description TEXT NOT NULL,
    endpoint_url VARCHAR(255) NOT NULL,
    owner_dept VARCHAR(100) NOT NULL,
    status VARCHAR(20) DEFAULT 'active',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 2. 路由调用审计日志表（核心看板数据源）
CREATE TABLE agent_call_logs (
    log_id BIGSERIAL PRIMARY KEY,
    session_id VARCHAR(100) NOT NULL,
    caller_dept VARCHAR(100) NOT NULL, -- 谁在用（调用方部门）
    target_agent_id VARCHAR(100) REFERENCES agents(agent_id), -- 用了谁的Agent
    tokens_used INT DEFAULT 0,
    latency_ms INT,
    status VARCHAR(20),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

```

### 5.2 看板核心指标计算公式 (Coding Agent 需实现聚合接口)

* **跨部门复用率 (Cross-Dept Reuse Rate)：**

$$\text{复用率} = \frac{\text{非本部门调用次数}}{\text{总调用次数}} \times 100\%$$


* **企业研发成本节省估算 (Cost Saved)：**

$$\text{节省人月} = \text{已注册的专家Agent数量} \times 0.5 \text{人月} + (\text{总调用次数} \times 0.05 \text{人月})$$



*(注：公式向老板证明：每多一个工具被大家复用，就免去了其他团队重复研发的成本。)*

---

## 6. Coding Agent 任务拆解与开发排期 (Todo List)

你可以命令 Coding Agent 按以下模块逐步交付代码：

* [ ] **Task 1: 基础设施与数据模型**
* 使用 FastAPI (Python) 或 Go 搭建骨架。
* 实现 `agents` 表和 `agent_logs` 表的 CRUD 操作。


* [ ] **Task 2: 注册服务与健康检查**
* 编写 `/api/v1/agents/register` 接口。
* 实现后台后台定时任务（Background Task），每隔 30 秒向所有 `health_check_url` 发送 Ping 请求，不通的更新状态为 `offline`。


* [ ] **Task 3: 双层路由引擎 (Core)**
* 编写一层路由函数 fast_route(user_query, agents_summary)，调用轻量模型，严格限制输出，处理 agent_id / COMPLEX / UNKNOWN 三种边界条件。
* 编写二层路由函数 deep_plan(user_query, full_agents_specs)，当一层返回 COMPLEX 时调用，并确保利用大模型的 JSON Mode（或结构化输出）解析出完整的 plan 数组。
* 实现调度器逻辑：遍历 plan，根据 depends_on 属性自动控制串行或并发请求专家 Agent，并将前序步骤的输出动态注入到后续步骤的 sub_query 环境中。


* [ ] **Task 4: 审计与看板 API**
* 每次路由请求成功后，异步将数据写入 `agent_call_logs`。
* 提供 `/api/v1/dashboard/metrics` 接口，返回各部门复用排行榜和大屏所需的数据结构。



---

这份文档精简地抽离了原本复杂的设计，完全聚焦在“协议、路由、复用度量”这三个核心价值点上。你可以直接把它丢给你的 Coding Agent 并对它说：

> **“请基于这份文档，使用 Python + FastAPI + PostgreSQL，先帮我把第一阶段和第二阶段的核心接口与数据库 Model 代码生成出来。”**