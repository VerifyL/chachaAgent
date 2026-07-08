import { useEffect, useState } from "react";
import { MessageList } from "./MessageList";
import { ChatInput } from "./ChatInput";
import { useChatStore } from "../store/chatStore";
import type { ClientMessage } from "../types";

interface Props {
  onSend: (msg: ClientMessage) => void;
  onStop: () => void;
}

export function ChatArea({ onSend, onStop }: Props) {
  const { sidebarOpen, toggleSidebar } = useChatStore();
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
      </div>

      {/* 消息列表 */}
      <MessageList />

      {/* 输入框 */}
      <ChatInput onSend={onSend} onStop={onStop} />
    </div>
  );
}
