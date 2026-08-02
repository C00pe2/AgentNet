import { useCallback, useEffect, useRef, useState } from "react";
import type {
  Artifact,
  MaskedAgentCard,
  SearchCandidate,
  TaskState,
} from "../api/types";
import {
  ApiError,
  appendMessage,
  authHeaders,
  cancelTask,
  createTask,
  searchAgents,
  sendFeedback,
  taskEventsUrl,
} from "../api/client";
import { subscribeSSE } from "../api/sse";
import { getConsumerKey } from "../api/keys";

// ---------- 聊天条目模型 ----------

type ChatItem =
  | { kind: "user"; id: string; text: string }
  | { kind: "agent"; id: string; text: string; streaming: boolean }
  | { kind: "routing"; id: string; query: string; candidates: SearchCandidate[]; selected: string }
  | { kind: "status"; id: string; state: TaskState }
  | { kind: "artifact"; id: string; artifact: Artifact }
  | { kind: "error"; id: string; text: string }
  | { kind: "system"; id: string; text: string; actionLabel?: string; onAction?: () => void }
  | { kind: "rating"; id: string; taskId: string; rated: number | null };

let seq = 0;
const nextId = () => `item-${++seq}`;

const TERMINAL: TaskState[] = ["completed", "failed", "canceled"];

interface Props {
  lockedAgent: MaskedAgentCard | null;
  onUnlock: () => void;
  onOpenSettings: () => void;
  onGoRegister: () => void;
}

export default function ChatPage({ lockedAgent, onUnlock, onOpenSettings, onGoRegister }: Props) {
  const [items, setItems] = useState<ChatItem[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [awaitingInput, setAwaitingInput] = useState(false);
  const [taskActive, setTaskActive] = useState(false);

  // 当前任务上下文
  const agentRef = useRef<MaskedAgentCard | null>(null);
  const taskIdRef = useRef<string | null>(null);
  const streamItemIdRef = useRef<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const listRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight });
  }, [items]);

  useEffect(() => () => abortRef.current?.abort(), []);

  const push = useCallback((item: ChatItem) => {
    setItems((prev) => [...prev, item]);
  }, []);

  const patchItem = useCallback((id: string, patch: Partial<ChatItem>) => {
    setItems((prev) => prev.map((it) => (it.id === id ? ({ ...it, ...patch } as ChatItem) : it)));
  }, []);

  const appendDelta = useCallback(
    (text: string) => {
      const existing = streamItemIdRef.current;
      if (existing) {
        setItems((prev) =>
          prev.map((it) =>
            it.id === existing && it.kind === "agent" ? { ...it, text: it.text + text } : it,
          ),
        );
      } else {
        const id = nextId();
        streamItemIdRef.current = id;
        push({ kind: "agent", id, text, streaming: true });
      }
    },
    [push],
  );

  const closeStreamBubble = useCallback(() => {
    const id = streamItemIdRef.current;
    if (id) patchItem(id, { streaming: false });
    streamItemIdRef.current = null;
  }, [patchItem]);

  const handleTerminal = useCallback(
    (state: TaskState) => {
      closeStreamBubble();
      setTaskActive(false);
      setAwaitingInput(false);
      setBusy(false);
      if (state === "completed" && taskIdRef.current) {
        push({ kind: "rating", id: nextId(), taskId: taskIdRef.current, rated: null });
      } else if (state === "failed") {
        // error 事件通常已给出原因;没有则补一条
        push({ kind: "system", id: nextId(), text: "任务失败。" });
      } else if (state === "canceled") {
        push({ kind: "system", id: nextId(), text: "任务已取消。" });
      }
    },
    [closeStreamBubble, push],
  );

  const runTask = useCallback(
    async (agent: MaskedAgentCard, query: string) => {
      agentRef.current = agent;
      let taskId: string;
      try {
        const task = await createTask(agent.agent_id, query);
        taskId = task.id;
      } catch (e) {
        if (e instanceof ApiError && e.status === 402) {
          push({ kind: "error", id: nextId(), text: `余额不足:${e.message}` });
        } else {
          push({
            kind: "error",
            id: nextId(),
            text: e instanceof Error ? e.message : "创建任务失败",
          });
        }
        return;
      }
      taskIdRef.current = taskId;
      setTaskActive(true);

      const ctrl = new AbortController();
      abortRef.current = ctrl;
      try {
        await subscribeSSE(taskEventsUrl(agent.agent_id, taskId), authHeaders(), {
          signal: ctrl.signal,
          onEvent: (ev) => {
            switch (ev.event) {
              case "state": {
                const s = ev.data.state;
                push({ kind: "status", id: nextId(), state: s });
                if (s === "input-required") {
                  closeStreamBubble();
                  setAwaitingInput(true);
                  setBusy(false);
                } else if (TERMINAL.includes(s)) {
                  handleTerminal(s);
                }
                break;
              }
              case "delta":
                appendDelta(ev.data.text);
                break;
              case "message": {
                closeStreamBubble();
                const text = ev.data.parts
                  .filter((p) => p.type === "text")
                  .map((p) => p.text)
                  .join("");
                push({ kind: "agent", id: nextId(), text, streaming: false });
                break;
              }
              case "artifact":
                push({ kind: "artifact", id: nextId(), artifact: ev.data });
                break;
              case "error":
                push({ kind: "error", id: nextId(), text: ev.data.error });
                break;
            }
          },
        });
      } catch (e) {
        if (!ctrl.signal.aborted) {
          push({
            kind: "error",
            id: nextId(),
            text: e instanceof Error ? e.message : "事件流中断",
          });
          handleTerminal("failed");
        }
      }
    },
    [appendDelta, closeStreamBubble, handleTerminal, push],
  );

  const handleSend = useCallback(async () => {
    const text = input.trim();
    if (!text || busy) return;

    if (!getConsumerKey()) {
      push({ kind: "system", id: nextId(), text: "请先在右上角「设置」中填写 Consumer API Key。" });
      onOpenSettings();
      return;
    }

    // 澄清模式:向等待输入的任务追加 message
    if (awaitingInput && agentRef.current && taskIdRef.current) {
      setInput("");
      push({ kind: "user", id: nextId(), text });
      setAwaitingInput(false);
      setBusy(true);
      try {
        await appendMessage(agentRef.current.agent_id, taskIdRef.current, text);
      } catch (e) {
        push({
          kind: "error",
          id: nextId(),
          text: e instanceof Error ? e.message : "发送澄清回答失败",
        });
        setBusy(false);
      }
      return;
    }

    if (taskActive) return; // 任务进行中,不允许开新任务

    setInput("");
    setBusy(true);
    push({ kind: "user", id: nextId(), text });

    let agent: MaskedAgentCard | null = lockedAgent;
    if (!agent) {
      try {
        const result = await searchAgents(text);
        if (result.candidates.length === 0) {
          push({
            kind: "system",
            id: nextId(),
            text: "网络中没有能回答这个问题的 agent。",
            actionLabel: "去注册一个",
            onAction: onGoRegister,
          });
          setBusy(false);
          return;
        }
        const top1 = result.candidates[0];
        push({
          kind: "routing",
          id: nextId(),
          query: text,
          candidates: result.candidates,
          selected: top1.card.agent_id,
        });
        agent = top1.card;
      } catch (e) {
        push({
          kind: "error",
          id: nextId(),
          text: e instanceof Error ? e.message : "召回失败",
        });
        setBusy(false);
        return;
      }
    } else {
      push({
        kind: "system",
        id: nextId(),
        text: `已锁定 agent「${agent.name}」,跳过自动路由。`,
      });
    }

    await runTask(agent, text);
    if (!taskIdRef.current) setBusy(false); // task 创建失败(runTask 内部已推错误)
  }, [input, busy, awaitingInput, taskActive, lockedAgent, push, runTask, onOpenSettings, onGoRegister]);

  const handleCancel = useCallback(async () => {
    if (!agentRef.current || !taskIdRef.current) return;
    try {
      await cancelTask(agentRef.current.agent_id, taskIdRef.current);
    } catch (e) {
      push({
        kind: "error",
        id: nextId(),
        text: e instanceof Error ? e.message : "取消失败",
      });
    }
  }, [push]);

  const handleRate = useCallback(
    async (itemId: string, taskId: string, rating: number) => {
      try {
        await sendFeedback(taskId, rating);
        patchItem(itemId, { rated: rating });
      } catch (e) {
        push({
          kind: "error",
          id: nextId(),
          text: e instanceof Error ? e.message : "评分提交失败",
        });
      }
    },
    [patchItem, push],
  );

  // 新任务开始时重置上下文(在 user 消息 push 前调用)
  const resetTaskContext = () => {
    taskIdRef.current = null;
    streamItemIdRef.current = null;
  };

  const sendWrapper = () => {
    if (!awaitingInput && !taskActive) resetTaskContext();
    void handleSend();
  };

  return (
    <div className="chat-page">
      {lockedAgent && (
        <div className="locked-banner">
          已锁定 agent:<strong>{lockedAgent.name}</strong>({lockedAgent.agent_id})
          <button className="link-btn" onClick={onUnlock}>
            解除锁定
          </button>
        </div>
      )}

      <div className="chat-list" ref={listRef}>
        {items.length === 0 && (
          <div className="empty-hint">
            向 AgentNet 网络提问 —— 会自动召回最合适的 agent 并流式返回答案。
          </div>
        )}
        {items.map((item) => {
          switch (item.kind) {
            case "user":
              return (
                <div key={item.id} className="bubble user">
                  {item.text}
                </div>
              );
            case "agent":
              return (
                <div key={item.id} className="bubble agent">
                  {item.text}
                  {item.streaming && <span className="cursor">▍</span>}
                </div>
              );
            case "routing":
              return (
                <div key={item.id} className="routing-card">
                  <div className="routing-title">
                    路由:召回 {item.candidates.length} 个候选,自动选择 top1
                  </div>
                  <ul>
                    {item.candidates.map((c) => (
                      <li
                        key={c.card.agent_id}
                        className={c.card.agent_id === item.selected ? "selected" : ""}
                      >
                        <span className="cand-name">
                          {c.card.agent_id === item.selected ? "→ " : ""}
                          {c.card.name}
                          <code>{c.card.agent_id}</code>
                        </span>
                        <span className="cand-score">相似度 {c.score.toFixed(3)}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              );
            case "status":
              return (
                <div key={item.id} className={`status-line state-${item.state}`}>
                  状态:{item.state}
                </div>
              );
            case "artifact":
              return (
                <div key={item.id} className="artifact-card">
                  <div className="artifact-name">产物:{item.artifact.name}</div>
                  {item.artifact.parts.map((p, i) => (
                    <pre key={i}>
                      {p.type === "text"
                        ? p.text
                        : p.type === "file"
                          ? (p.data ?? p.url ?? p.name)
                          : JSON.stringify(p.data, null, 2)}
                    </pre>
                  ))}
                </div>
              );
            case "error":
              return (
                <div key={item.id} className="bubble error">
                  {item.text}
                </div>
              );
            case "system":
              return (
                <div key={item.id} className="system-line">
                  {item.text}
                  {item.actionLabel && item.onAction && (
                    <button className="link-btn" onClick={item.onAction}>
                      {item.actionLabel}
                    </button>
                  )}
                </div>
              );
            case "rating":
              return (
                <div key={item.id} className="rating-card">
                  {item.rated === null ? (
                    <>
                      <span>任务完成,为本次回答评分:</span>
                      {[1, 2, 3, 4, 5].map((n) => (
                        <button
                          key={n}
                          className="star-btn"
                          onClick={() => void handleRate(item.id, item.taskId, n)}
                        >
                          ★
                        </button>
                      ))}
                      <button
                        className="link-btn"
                        onClick={() => void handleRate(item.id, item.taskId, 0)}
                      >
                        0 星
                      </button>
                    </>
                  ) : (
                    <span>
                      已评分:{item.rated} / 5 {"★".repeat(item.rated)}
                    </span>
                  )}
                </div>
              );
          }
        })}
      </div>

      <div className="chat-input-row">
        <textarea
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              sendWrapper();
            }
          }}
          placeholder={
            awaitingInput
              ? "agent 等待你的澄清回答…"
              : taskActive
                ? "任务进行中…"
                : "输入问题,Enter 发送(Shift+Enter 换行)"
          }
          disabled={taskActive && !awaitingInput}
          rows={2}
        />
        {taskActive ? (
          awaitingInput ? (
            <div className="btn-col">
              <button onClick={sendWrapper} disabled={busy || !input.trim()}>
                回答
              </button>
              <button className="danger" onClick={() => void handleCancel()}>
                取消任务
              </button>
            </div>
          ) : (
            <button className="danger" onClick={() => void handleCancel()}>
              取消任务
            </button>
          )
        ) : (
          <button onClick={sendWrapper} disabled={busy || !input.trim()}>
            {busy ? "发送中…" : "发送"}
          </button>
        )}
      </div>
    </div>
  );
}
