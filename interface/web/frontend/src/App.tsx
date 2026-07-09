import { useEffect, useCallback } from "react";
import { useChatStore } from "./store/chatStore";
import { useWebSocket } from "./hooks/useWebSocket";
import { Sidebar } from "./components/Sidebar";
import { ChatArea } from "./components/ChatArea";
import { ApprovalDialog } from "./components/ApprovalDialog";
import type { ChatMessage, ClientMessage } from "./types";

let msgSeq = 0;

function App() {
  const { send, sendChat, sendCompact, stop } = useWebSocket();
  const {
    darkMode,
    addMessage,
    setStreaming,
    clearMessages,
    setRetry,
    lastUserInput,
    setError,
  } = useChatStore();

  // 暗/亮主题切换
  useEffect(() => {
    document.documentElement.classList.toggle("dark", darkMode);
  }, [darkMode]);

  // 构建 retry 函数
  const handleSend = useCallback(
    (content: string) => {
      const userMsg: ChatMessage = {
        id: `msg-${++msgSeq}`,
        role: "user",
        content,
        toolCalls: [],
        timestamp: Date.now(),
      };
      addMessage(userMsg);

      const assistantMsg: ChatMessage = {
        id: `msg-${++msgSeq}`,
        role: "assistant",
        content: "",
        toolCalls: [],
        timestamp: Date.now(),
      };
      addMessage(assistantMsg);

      setStreaming(true);
      sendChat(content);
    },
    [addMessage, setStreaming, sendChat],
  );

  // 注入 retry 到 store
  useEffect(() => {
    setRetry(() => {
      if (lastUserInput) {
        setError(null);
        handleSend(lastUserInput);
      }
    });
  }, [setRetry, lastUserInput, handleSend, setError]);

  const onSend = useCallback(
    (msg: ClientMessage) => {
      if (msg.type !== "chat") return;
      handleSend(msg.content);
    },
    [handleSend],
  );

  // 新建会话：清空消息 + 通过 WebSocket 创建新 session
  const handleNewSession = useCallback(() => {
    clearMessages();
    send({ type: "new_session" });
  }, [clearMessages, send]);

  return (
    <div className="flex h-screen w-screen overflow-hidden">
      <Sidebar onNewSession={handleNewSession} />
      <ChatArea onSend={onSend} onStop={stop} onCompact={sendCompact} />
      <ApprovalDialog />
    </div>
  );
}

export default App;
