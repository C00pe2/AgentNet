import { useCallback, useEffect, useState } from "react";
import type { MaskedAgentCard, Reputation, SearchCandidate } from "../api/types";
import { listAgents, searchAgents } from "../api/client";
import { getConsumerKey, getProviderKey } from "../api/keys";

interface Props {
  onAsk: (agent: MaskedAgentCard) => void; // 锁定 agent 并跳到聊天页
  onOpenSettings: () => void;
  onGoRegister: () => void;
}

function RepLine({ rep }: { rep: Reputation | null | undefined }) {
  if (!rep) return <span className="muted">暂无信誉数据</span>;
  return (
    <span className="rep-line">
      评分 {rep.rating === null ? "—" : rep.rating.toFixed(1)} · 调用 {rep.calls} 次 · 成功率{" "}
      {(rep.success_rate * 100).toFixed(0)}% · 均延 {rep.avg_latency_ms.toFixed(0)}ms
      {rep.canary_score !== null && <> · canary {(rep.canary_score * 100).toFixed(0)}%</>}
    </span>
  );
}

function AgentDetailModal({
  agent,
  onClose,
  onAsk,
}: {
  agent: MaskedAgentCard;
  onClose: () => void;
  onAsk: (a: MaskedAgentCard) => void;
}) {
  return (
    <div className="modal-mask" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h3>
            {agent.name} <code>{agent.agent_id}</code>
          </h3>
          <button className="link-btn" onClick={onClose}>
            关闭
          </button>
        </div>
        {agent.status && (
          <div className={`status-badge ${agent.status}`}>
            {agent.status === "active" ? "在线" : "离线"}
          </div>
        )}
        <p>{agent.description || "（无描述）"}</p>
        {agent.natural_capabilities && (
          <div className="detail-block">
            <div className="detail-label">能力描述</div>
            <p>{agent.natural_capabilities}</p>
          </div>
        )}
        {agent.capabilities && agent.capabilities.length > 0 && (
          <div className="tag-row">
            {agent.capabilities.map((c) => (
              <span key={c} className="tag">
                {c}
              </span>
            ))}
        </div>
        )}
        <div className="detail-block">
          <div className="detail-label">信誉</div>
          <RepLine rep={agent.reputation} />
        </div>
        <div className="detail-block">
          <div className="detail-label">计费</div>
          {agent.pricing?.model === "per-call"
            ? `按次收费 ${agent.pricing.price} 积分`
            : "免费"}
        </div>
        <div className="detail-block">
          <div className="detail-label">其他</div>
          <div className="muted">
            版本 {agent.version ?? "—"} · Provider {agent.provider ?? "—"} · canary 用例{" "}
            {agent.canary_cases?.length ?? 0} 条
          </div>
        </div>
        <button className="primary" onClick={() => onAsk(agent)}>
          向它提问
        </button>
      </div>
    </div>
  );
}

export default function AgentsPage({ onAsk, onOpenSettings, onGoRegister }: Props) {
  const [agents, setAgents] = useState<MaskedAgentCard[]>([]);
  const [searchResults, setSearchResults] = useState<SearchCandidate[] | null>(null);
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [detail, setDetail] = useState<MaskedAgentCard | null>(null);

  const load = useCallback(async () => {
    setError("");
    setLoading(true);
    try {
      setAgents(await listAgents());
    } catch (e) {
      setError(e instanceof Error ? e.message : "加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!getConsumerKey() && !getProviderKey()) {
      setError("请先在「设置」中填写 API Key。");
      return;
    }
    void load();
  }, [load]);

  const doSearch = useCallback(async () => {
    const q = query.trim();
    if (!q) {
      setSearchResults(null);
      return;
    }
    setError("");
    setLoading(true);
    try {
      const result = await searchAgents(q);
      setSearchResults(result.candidates);
    } catch (e) {
      setError(e instanceof Error ? e.message : "搜索失败");
    } finally {
      setLoading(false);
    }
  }, [query]);

  const showing: Array<{ card: MaskedAgentCard; score: number | null }> = searchResults
    ? searchResults.map((c) => ({ card: c.card, score: c.score }))
    : agents.map((a) => ({ card: a, score: null }));

  return (
    <div className="agents-page">
      <div className="agents-toolbar">
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") void doSearch();
          }}
          placeholder="语义搜索 agent 能力…"
        />
        <button onClick={() => void doSearch()} disabled={loading}>
          搜索
        </button>
        {searchResults && (
          <button
            className="link-btn"
            onClick={() => {
              setSearchResults(null);
              setQuery("");
            }}
          >
            返回列表
          </button>
        )}
        <button className="link-btn" onClick={() => void load()} disabled={loading}>
          刷新
        </button>
        {searchResults && (
          <span className="muted">搜索命中 {searchResults.length} 个(按相似度排序)</span>
        )}
      </div>

      {error && (
        <div className="error-line">
          {error}
          {error.includes("API Key") && (
            <button className="link-btn" onClick={onOpenSettings}>
              去设置
            </button>
          )}
        </div>
      )}
      {loading && <div className="muted">加载中…</div>}

      {!loading && showing.length === 0 && !error && (
        <div className="empty-hint">
          {searchResults ? (
            "没有匹配的 agent。"
          ) : (
            <>
              <p>网络中还没有任何 agent。</p>
              <p className="muted">在本地跑一个 agent 服务,然后把它注册进来。</p>
              <button className="primary" onClick={onGoRegister}>
                注册你的第一个 agent
              </button>
            </>
          )}
        </div>
      )}

      <div className="agent-grid">
        {showing.map(({ card, score }) => (
          <div key={card.agent_id} className="agent-card" onClick={() => setDetail(card)}>
            <div className="agent-card-header">
              <strong>{card.name}</strong>
              {card.status && (
                <span className={`status-badge ${card.status}`}>
                  {card.status === "active" ? "在线" : "离线"}
                </span>
              )}
              {score !== null && <span className="cand-score">相似度 {score.toFixed(3)}</span>}
            </div>
            <code className="agent-id">{card.agent_id}</code>
            <p className="agent-desc">{card.description || "（无描述）"}</p>
            {card.capabilities && card.capabilities.length > 0 && (
              <div className="tag-row">
                {card.capabilities.map((c) => (
                  <span key={c} className="tag">
                    {c}
                  </span>
                ))}
              </div>
            )}
            <RepLine rep={card.reputation} />
            <div className="agent-card-actions">
              <button
                className="primary"
                onClick={(e) => {
                  e.stopPropagation();
                  onAsk(card);
                }}
              >
                向它提问
              </button>
            </div>
          </div>
        ))}
      </div>

      {detail && (
        <AgentDetailModal
          agent={detail}
          onClose={() => setDetail(null)}
          onAsk={(a) => {
            setDetail(null);
            onAsk(a);
          }}
        />
      )}
    </div>
  );
}
