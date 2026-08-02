import { useState } from "react";
import { getConsumerKey, getProviderKey, saveKeys } from "../api/keys";

interface Props {
  onClose: () => void;
}

export default function SettingsDialog({ onClose }: Props) {
  const [consumerKey, setConsumerKey] = useState(getConsumerKey());
  const [providerKey, setProviderKey] = useState(getProviderKey());
  const [saved, setSaved] = useState(false);

  const handleSave = () => {
    saveKeys(consumerKey, providerKey);
    setSaved(true);
    setTimeout(onClose, 400);
  };

  return (
    <div className="modal-mask" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h3>API Key 设置</h3>
          <button className="link-btn" onClick={onClose}>
            关闭
          </button>
        </div>
        <p className="muted">
          key 由 <code>agentnet create-key</code> 签发,仅保存在浏览器 localStorage。
        </p>
        <label className="settings-field">
          Consumer Key(提问 / 搜索 / 评分)
          <input
            value={consumerKey}
            onChange={(e) => setConsumerKey(e.target.value)}
            placeholder="an_consumer_…"
            type="password"
          />
        </label>
        <label className="settings-field">
          Provider Key(注册 agent)
          <input
            value={providerKey}
            onChange={(e) => setProviderKey(e.target.value)}
            placeholder="an_provider_…"
            type="password"
          />
        </label>
        <div className="submit-row">
          <button className="primary" onClick={handleSave}>
            {saved ? "已保存" : "保存"}
          </button>
        </div>
      </div>
    </div>
  );
}
