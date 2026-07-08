import { useState, useRef, useEffect, type KeyboardEvent } from "react";
import { useChatStore } from "../store/chatStore";
import type { ClientMessage } from "../types";

interface Props {
  onSend: (msg: ClientMessage) => void;
  onStop: () => void;
}

export function ChatInput({ onSend, onStop }: Props) {
  const [input, setInput] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const { streaming } = useChatStore();

  const handleSend = () => {
    const content = input.trim();
    if (!content || streaming) return;
    setInput("");
    onSend({ type: "chat", content });
  };

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter") {
      if (e.shiftKey) {
        // Shift+Enter 换行（默认行为，不拦截）
        return;
      }
      // Enter 发送
      e.preventDefault();
      handleSend();
    }
  };

  // 自动调整高度
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 200) + "px";
  }, [input]);

  return (
    <div
      className="p-4 border-t"
      style={{ borderColor: "var(--border-color)" }}
    >
      <div className="flex items-end gap-2 max-w-3xl mx-auto">
        <textarea
          ref={textareaRef}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="输入消息，Enter 发送，Shift+Enter 换行..."
          rows={1}
          disabled={streaming}
          className="flex-1 resize-none rounded-xl px-4 py-3 text-sm outline-none border"
          style={{
            backgroundColor: "var(--bg-tertiary)",
            borderColor: "var(--border-color)",
            color: "var(--text-primary)",
          }}
        />
        <button
          onClick={streaming ? onStop : handleSend}
          disabled={!streaming && (!input.trim() || streaming)}
          className="px-4 py-3 rounded-xl text-sm font-medium transition-opacity disabled:opacity-50"
          style={{
            backgroundColor: "var(--accent)",
            color: "#fff",
          }}
        >
          {streaming ? "■" : "→"}
        </button>
      </div>
      <p
        className="text-center text-xs mt-2"
        style={{ color: "var(--text-secondary)" }}
      >
        Enter 发送 · Shift+Enter 换行
      </p>
    </div>
  );
}
