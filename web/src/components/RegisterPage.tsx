import { useState } from "react";
import type { AgentCard, CanaryCase } from "../api/types";
import { registerAgent } from "../api/client";
import { getProviderKey } from "../api/keys";

const AGENT_ID_RE = /^[a-zA-Z0-9_.\-]{1,64}$/;

interface CanaryRow {
  query: string;
  expect: string; // 逗号分隔,提交时转 string[]
}

interface Props {
  onOpenSettings: () => void;
}

export default function RegisterPage({ onOpenSettings }: Props) {
  const [agentId, setAgentId] = useState("");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [naturalCaps, setNaturalCaps] = useState("");
  const [capabilities, setCapabilities] = useState(""); // 逗号分隔
  const [endpoint, setEndpoint] = useState("");
  const [authType, setAuthType] = useState<"none" | "bearer">("none");
  const [authToken, setAuthToken] = useState("");
  const [pricingModel, setPricingModel] = useState<"free" | "per-call">("free");
  const [price, setPrice] = useState("");
  const [canaryRows, setCanaryRows] = useState<CanaryRow[]>([]);

  const [submitting, setSubmitting] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);

  const buildCard = (): AgentCard | string => {
    if (!AGENT_ID_RE.test(agentId)) {
      return "agent_id 不合法:仅允许字母/数字/_/./-,长度 1~64。";
    }
    if (!name.trim()) return "name 必填。";
    if (!/^https?:\/\/.+/.test(endpoint.trim()))
      return "endpoint 必填,且必须是 http(s):// URL。";
    if (pricingModel === "per-call") {
      const p = Number(price);
      if (!price.trim() || Number.isNaN(p) || p <= 0)
        return "按次收费时必须填写大于 0 的 price(积分)。";
    }

    const canary: CanaryCase[] = canaryRows
      .filter((r) => r.query.trim())
      .map((r) => ({
        query: r.query.trim(),
        expect: r.expect
          .split(/[,，]/)
          .map((s) => s.trim())
          .filter(Boolean),
      }));
    for (const c of canary) {
      if (c.expect.length === 0) return "canary 用例的 expect 至少需要一个关键词。";
    }

    return {
      agent_id: agentId.trim(),
      name: name.trim(),
      description: description.trim(),
      natural_capabilities: naturalCaps.trim(),
      capabilities: capabilities
        .split(/[,，]/)
        .map((s) => s.trim())
        .filter(Boolean),
      endpoint: endpoint.trim().replace(/\/+$/, ""),
      auth:
        authType === "bearer"
          ? { type: "bearer", token: authToken.trim() || null }
          : { type: "none", token: null },
      pricing:
        pricingModel === "per-call"
          ? { model: "per-call", price: Number(price), currency: "CNY" }
          : { model: "free", price: null, currency: "CNY" },
      canary_cases: canary,
    };
  };

  const handleSubmit = async () => {
    setResult(null);
    if (!getProviderKey()) {
      setResult({ ok: false, text: "请先在「设置」中填写 Provider API Key。" });
      onOpenSettings();
      return;
    }
    const card = buildCard();
    if (typeof card === "string") {
      setResult({ ok: false, text: card });
      return;
    }
    setSubmitting(true);
    try {
      const resp = await registerAgent(card);
      setResult({
        ok: true,
        text: `注册成功:${resp.agent_id}(registry 已回调 ${endpoint}/card 验证通过)`,
      });
    } catch (e) {
      setResult({
        ok: false,
        text: e instanceof Error ? e.message : "注册失败",
      });
    } finally {
      setSubmitting(false);
    }
  };

  const patchCanary = (idx: number, patch: Partial<CanaryRow>) => {
    setCanaryRows((rows) => rows.map((r, i) => (i === idx ? { ...r, ...patch } : r)));
  };

  return (
    <div className="register-page">
      <div className="form-grid">
        <label>
          agent_id <span className="req">*</span>
          <input
            value={agentId}
            onChange={(e) => setAgentId(e.target.value)}
            placeholder="如 go-reviewer(字母/数字/_/./-)"
          />
        </label>
        <label>
          名称 <span className="req">*</span>
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="如 Go Reviewer" />
        </label>
        <label className="span2">
          描述
          <input
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="一句话说明这个 agent 是做什么的"
          />
        </label>
        <label className="span2">
          自然语言能力描述(embedding 召回的主要依据)
          <textarea
            value={naturalCaps}
            onChange={(e) => setNaturalCaps(e.target.value)}
            rows={3}
            placeholder="我擅长审查 Go 代码:goroutine 泄漏、channel 死锁、竞态条件…"
          />
        </label>
        <label className="span2">
          能力标签(逗号分隔)
          <input
            value={capabilities}
            onChange={(e) => setCapabilities(e.target.value)}
            placeholder="go, code-review, concurrency"
          />
        </label>
        <label className="span2">
          endpoint <span className="req">*</span>
          <input
            value={endpoint}
            onChange={(e) => setEndpoint(e.target.value)}
            placeholder="http://localhost:8001(注册时 registry 会回调 /card 验证)"
          />
        </label>
        <label>
          agent 鉴权
          <select value={authType} onChange={(e) => setAuthType(e.target.value as "none" | "bearer")}>
            <option value="none">none(无鉴权)</option>
            <option value="bearer">bearer(registry 持 token 转发)</option>
          </select>
        </label>
        {authType === "bearer" && (
          <label>
            bearer token
            <input
              value={authToken}
              onChange={(e) => setAuthToken(e.target.value)}
              placeholder="agent 侧校验的 token(不下发给消费者)"
            />
          </label>
        )}
        <label>
          计费模式
          <select
            value={pricingModel}
            onChange={(e) => setPricingModel(e.target.value as "free" | "per-call")}
          >
            <option value="free">free(免费)</option>
            <option value="per-call">per-call(按次收积分)</option>
          </select>
        </label>
        {pricingModel === "per-call" && (
          <label>
            单次价格(积分)
            <input
              value={price}
              onChange={(e) => setPrice(e.target.value)}
              placeholder="如 1"
              inputMode="numeric"
            />
          </label>
        )}

        <div className="span2 canary-section">
          <div className="detail-label">
            canary 用例(registry 定期跑分验证能力真实性)
            <button
              className="link-btn"
              onClick={() => setCanaryRows((r) => [...r, { query: "", expect: "" }])}
            >
              + 添加用例
            </button>
          </div>
          {canaryRows.map((row, i) => (
            <div key={i} className="canary-row">
              <input
                value={row.query}
                onChange={(e) => patchCanary(i, { query: e.target.value })}
                placeholder="测试 query"
              />
              <input
                value={row.expect}
                onChange={(e) => patchCanary(i, { expect: e.target.value })}
                placeholder="期望关键词,逗号分隔"
              />
              <button
                className="link-btn danger-text"
                onClick={() => setCanaryRows((rows) => rows.filter((_, j) => j !== i))}
              >
                删除
              </button>
            </div>
          ))}
        </div>
      </div>

      <div className="submit-row">
        <button className="primary" onClick={() => void handleSubmit()} disabled={submitting}>
          {submitting ? "注册中…" : "注册 Agent"}
        </button>
        {result && (
          <span className={result.ok ? "ok-text" : "error-text"}>{result.text}</span>
        )}
      </div>
    </div>
  );
}
