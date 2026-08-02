import type {
  AgentCard,
  Envelope,
  MaskedAgentCard,
  SearchResult,
  Task,
  TaskCreate,
  MessageAppend,
} from "./types";
import { getConsumerKey, getProviderKey } from "./keys";

// 后端错误:携带 envelope 的 message 与 http 状态码(402/429/503…前端要区分展示)。
export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(
  method: string,
  path: string,
  apiKey: string,
  body?: unknown,
): Promise<T> {
  const headers: Record<string, string> = {};
  if (apiKey) headers["Authorization"] = `Bearer ${apiKey}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";

  let resp: Response;
  try {
    resp = await fetch(path, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new ApiError("无法连接 registry,请确认它已启动(默认 :9000)", 0);
  }

  let envelope: Envelope<T>;
  try {
    envelope = (await resp.json()) as Envelope<T>;
  } catch {
    throw new ApiError(`registry 返回了非 JSON 响应(HTTP ${resp.status})`, resp.status);
  }

  if (!resp.ok || envelope.code !== 0) {
    throw new ApiError(envelope.message || `请求失败(HTTP ${resp.status})`, resp.status);
  }
  return envelope.data as T;
}

// ---------- 控制面 ----------

export function searchAgents(q: string, topK = 10): Promise<SearchResult> {
  const params = new URLSearchParams({ q, top_k: String(topK) });
  return request("GET", `/v1/agents/search?${params}`, getConsumerKey());
}

export function listAgents(): Promise<MaskedAgentCard[]> {
  // 任何合法 key 均可;优先 consumer,空则退 provider。
  return request("GET", "/v1/agents", getConsumerKey() || getProviderKey());
}

export function getAgent(agentId: string): Promise<MaskedAgentCard> {
  return request(
    "GET",
    `/v1/agents/${encodeURIComponent(agentId)}`,
    getConsumerKey() || getProviderKey(),
  );
}

export function registerAgent(card: AgentCard): Promise<{ agent_id: string; status: string }> {
  return request("POST", "/v1/agents", getProviderKey(), card);
}

export function sendFeedback(taskId: string, rating: number): Promise<unknown> {
  return request("POST", "/v1/feedback", getConsumerKey(), {
    task_id: taskId,
    rating,
  });
}

// ---------- 数据面(Gateway 代理) ----------

export function createTask(agentId: string, query: string): Promise<Task> {
  const body: TaskCreate = {
    message: { role: "user", parts: [{ type: "text", text: query }] },
  };
  return request(
    "POST",
    `/v1/agents/${encodeURIComponent(agentId)}/tasks`,
    getConsumerKey(),
    body,
  );
}

export function appendMessage(agentId: string, taskId: string, text: string): Promise<Task> {
  const body: MessageAppend = {
    message: { role: "user", parts: [{ type: "text", text }] },
  };
  return request(
    "POST",
    `/v1/agents/${encodeURIComponent(agentId)}/tasks/${encodeURIComponent(taskId)}/messages`,
    getConsumerKey(),
    body,
  );
}

export function cancelTask(agentId: string, taskId: string): Promise<Task> {
  return request(
    "POST",
    `/v1/agents/${encodeURIComponent(agentId)}/tasks/${encodeURIComponent(taskId)}/cancel`,
    getConsumerKey(),
    {},
  );
}

export function getTask(agentId: string, taskId: string): Promise<Task> {
  return request(
    "GET",
    `/v1/agents/${encodeURIComponent(agentId)}/tasks/${encodeURIComponent(taskId)}`,
    getConsumerKey(),
  );
}

// SSE 订阅需要的鉴权 header(供 sse.ts 使用)。
export function authHeaders(): Record<string, string> {
  const key = getConsumerKey();
  return key ? { Authorization: `Bearer ${key}` } : {};
}

export function taskEventsUrl(agentId: string, taskId: string): string {
  return `/v1/agents/${encodeURIComponent(agentId)}/tasks/${encodeURIComponent(taskId)}/events`;
}
