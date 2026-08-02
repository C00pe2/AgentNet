// fetch + ReadableStream 手写 SSE 解析(EventSource 无法带 Authorization header)。
// 线路格式:event: {name}\ndata: {单行 JSON}\n\n;以 ":" 开头的行是心跳/注释,丢弃。

import type { SseEvent } from "./types";

export interface SubscribeOptions {
  signal?: AbortSignal;
  onEvent: (ev: SseEvent) => void;
}

// 解析 SSE 文本流,逐事件回调。网络错误会 throw,由调用方处理。
export async function subscribeSSE(
  url: string,
  headers: Record<string, string>,
  opts: SubscribeOptions,
): Promise<void> {
  const resp = await fetch(url, { headers, signal: opts.signal });
  if (!resp.ok || !resp.body) {
    let msg = `订阅事件流失败(HTTP ${resp.status})`;
    try {
      const body = (await resp.json()) as { message?: string };
      if (body.message) msg = body.message;
    } catch {
      // 保持默认 msg
    }
    throw new Error(msg);
  }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let eventName = "";
  let dataLines: string[] = [];

  const dispatch = () => {
    if (!eventName || dataLines.length === 0) {
      eventName = "";
      dataLines = [];
      return;
    }
    const raw = dataLines.join("\n");
    try {
      const data = JSON.parse(raw);
      opts.onEvent({ event: eventName, data } as SseEvent);
    } catch {
      // 非 JSON data,忽略
    }
    eventName = "";
    dataLines = [];
  };

  const handleLine = (line: string) => {
    if (line === "") {
      dispatch();
      return;
    }
    if (line.startsWith(":")) return; // 心跳/注释
    const idx = line.indexOf(":");
    const field = idx === -1 ? line : line.slice(0, idx);
    let value = idx === -1 ? "" : line.slice(idx + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") eventName = value;
    else if (field === "data") dataLines.push(value);
  };

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let nl: number;
    while ((nl = buffer.indexOf("\n")) !== -1) {
      let line = buffer.slice(0, nl);
      buffer = buffer.slice(nl + 1);
      if (line.endsWith("\r")) line = line.slice(0, -1);
      handleLine(line);
    }
  }
  // 流末尾冲刷残留
  if (buffer.length > 0) handleLine(buffer);
  dispatch();
}
