// 与 agentnet-core / agentnet-registry 的 pydantic 模型一一对应。

// ---------- 通用 envelope ----------
export interface Envelope<T> {
  code: number;
  message: string;
  data: T | null;
}

// ---------- AgentCard ----------
export interface AgentAuth {
  type?: "none" | "bearer";
  token?: string | null;
}

export interface AgentPricing {
  model?: "free" | "per-call";
  price?: number | null;
  currency?: string;
}

export interface CanaryCase {
  query: string;
  expect: string[];
}

export interface Reputation {
  calls: number;
  success_rate: number;
  avg_latency_ms: number;
  rating: number | null;
  canary_score: number | null;
}

export interface AgentCard {
  agent_id: string;
  name: string;
  description?: string;
  natural_capabilities?: string;
  capabilities?: string[];
  endpoint?: string;
  auth?: AgentAuth;
  pricing?: AgentPricing;
  canary_cases?: CanaryCase[];
  version?: string;
  provider?: string | null;
  reputation?: Reputation | null;
}

// 消费者视角:endpoint/auth 被掩码;list/detail 额外带 status(search 结果没有)。
export type AgentStatus = "active" | "offline";

export interface MaskedAgentCard extends AgentCard {
  status?: AgentStatus;
}

// ---------- search ----------
export interface SearchCandidate {
  score: number; // 余弦相似度 0~1
  card: MaskedAgentCard;
}

export interface SearchResult {
  query: string;
  candidates: SearchCandidate[];
}

// ---------- Task / Message / Part / Artifact ----------
export type TaskState =
  | "submitted"
  | "working"
  | "input-required"
  | "completed"
  | "failed"
  | "canceled";

export type MessageRole = "user" | "agent";

export interface TextPart {
  type: "text";
  text: string;
}

export interface FilePart {
  type: "file";
  name: string;
  mime_type?: string;
  url?: string | null;
  data?: string | null;
}

export interface DataPart {
  type: "data";
  data: Record<string, unknown>;
}

export type Part = TextPart | FilePart | DataPart;

export interface Message {
  role: MessageRole;
  parts: Part[];
  created_at?: string;
}

export interface Artifact {
  name: string;
  parts: Part[];
  created_at?: string;
}

export interface Task {
  id: string;
  agent_id?: string;
  state?: TaskState;
  messages?: Message[];
  artifacts?: Artifact[];
  error?: string | null;
  created_at?: string;
  updated_at?: string;
}

export interface TaskCreate {
  message: Message;
  context?: Record<string, unknown>;
}

export interface MessageAppend {
  message: Message;
}

// ---------- feedback ----------
export interface FeedbackRequest {
  task_id: string;
  rating: number; // 0~5
}

// ---------- SSE 事件 ----------
export interface StateEvent {
  state: TaskState;
}

export interface DeltaEvent {
  text: string;
}

export interface ErrorEvent {
  error: string;
}

export type SseEvent =
  | { event: "state"; data: StateEvent }
  | { event: "delta"; data: DeltaEvent }
  | { event: "message"; data: Message }
  | { event: "artifact"; data: Artifact }
  | { event: "error"; data: ErrorEvent };
