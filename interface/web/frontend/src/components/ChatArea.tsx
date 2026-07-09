import { useEffect, useState } from "react";
import { MessageList } from "./MessageList";
import { ChatInput } from "./ChatInput";
import { useChatStore } from "../store/chatStore";
import type { ClientMessage } from "../types";

interface Props {
  onSend: (msg: ClientMessage) => void;
  onStop: () => void;
  onCompact: () => void;
}

export function ChatArea({ onSend, onStop, onCompact }: Props) {
  const { sidebarOpen, toggleSidebar, toast } = useChatStore();
  const [isMobile, setIsMobile] = useState(window.innerWidth < 768);

  useEffect(() => {
    const onResize = () => setIsMobile(window.innerWidth < 768);
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  return (
    <div className="flex flex-col flex-1 min-w-0 h-full">
      {/* 顶部栏 */}
      <div
        className="flex items-center h-12 px-4 border-b flex-shrink-0"
        style={{ borderColor: "var(--border-color)" }}
      >
        {(!sidebarOpen || isMobile) && (
          <button
            onClick={toggleSidebar}
            className="mr-3 p-1 rounded hover:opacity-70 text-lg"
            title="展开侧边栏"
          >
            {isMobile ? "☰" : "▶"}
          </button>
        )}
        <span className="text-sm font-medium">对话</span>
        <div className="flex-1" />
        <button
          onClick={onCompact}
          className="px-2 py-0.5 text-xs rounded border hover:opacity-70"
          style={{ borderColor: "var(--border-color)" }}
          title="压缩上下文（减少 token 消耗）"
        >
          🗜️ 压缩
        </button>
      </div>

      {/* Toast 提示 */}
      {toast && (
        <div className="px-4 py-1.5 text-xs text-center"
          style={{ backgroundColor: "#dcfce7", color: "#166534" }}>
          {toast}
        </div>
      )}

      {/* 消息列表 */}
      <MessageList />

      {/* 输入框 */}
      <ChatInput onSend={onSend} onStop={onStop} />
    </div>
  );
}
