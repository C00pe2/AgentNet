import { useState } from "react";
import type { MaskedAgentCard } from "./api/types";
import ChatPage from "./components/ChatPage";
import AgentsPage from "./components/AgentsPage";
import RegisterPage from "./components/RegisterPage";
import SettingsDialog from "./components/SettingsDialog";

type Tab = "chat" | "agents" | "register";

export default function App() {
  const [tab, setTab] = useState<Tab>("chat");
  const [lockedAgent, setLockedAgent] = useState<MaskedAgentCard | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);

  const handleAsk = (agent: MaskedAgentCard) => {
    setLockedAgent(agent);
    setTab("chat");
  };

  return (
    <div className="app-shell">
      <header className="app-header">
        <div className="brand">
          AgentNet <span className="muted">控制台</span>
        </div>
        <nav>
          <button className={tab === "chat" ? "tab active" : "tab"} onClick={() => setTab("chat")}>
            聊天
          </button>
          <button
            className={tab === "agents" ? "tab active" : "tab"}
            onClick={() => setTab("agents")}
          >
            Agents
          </button>
          <button
            className={tab === "register" ? "tab active" : "tab"}
            onClick={() => setTab("register")}
          >
            注册 Agent
          </button>
        </nav>
        <button className="link-btn" onClick={() => setSettingsOpen(true)}>
          设置
        </button>
      </header>

      <main>
        {tab === "chat" && (
          <ChatPage
            lockedAgent={lockedAgent}
            onUnlock={() => setLockedAgent(null)}
            onOpenSettings={() => setSettingsOpen(true)}
            onGoRegister={() => setTab("register")}
          />
        )}
        {tab === "agents" && (
          <AgentsPage
            onAsk={handleAsk}
            onOpenSettings={() => setSettingsOpen(true)}
            onGoRegister={() => setTab("register")}
          />
        )}
        {tab === "register" && <RegisterPage onOpenSettings={() => setSettingsOpen(true)} />}
      </main>

      {settingsOpen && <SettingsDialog onClose={() => setSettingsOpen(false)} />}
    </div>
  );
}
