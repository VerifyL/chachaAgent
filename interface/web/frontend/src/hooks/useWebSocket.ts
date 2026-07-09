import { useEffect, useRef, useCallback } from "react";
import { useChatStore } from "../store/chatStore";
import type { ServerMessage, ClientMessage, ToolCallCard } from "../types";

let seq = 0;

export function useWebSocket() {
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | undefined>(
    undefined,
  );

  const {
    appendContent,
    appendReasoning,
    addToolCall,
    updateLastToolCall,
    setLastRunningToolId,
    setStreaming,
    setSessionId,
    setError,
    setLastUserInput,
    addSession,
  } = useChatStore();

  const handleServerMessage = useCallback((msg: ServerMessage) => {
    switch (msg.type) {
      case "session_created": {
        setSessionId(msg.session_id);
        // 判断是恢复已有会话还是真正新建
        const lastSid = localStorage.getItem("lastSessionId");
        const isRestore = lastSid === msg.session_id;
        localStorage.setItem("lastSessionId", msg.session_id);

        if (!isRestore) {
          // 真正的新会话 → 加入侧边栏列表
          addSession({
            id: msg.session_id,
            preview: "（新对话）",
            time: new Date().toLocaleString(),
          });
        }
        // 恢复已有会话 → fetchSessions 会填充列表，不重复添加
        break;
      }

      case "text":
        appendContent(msg.content);
        break;

      case "reasoning":
        appendReasoning(msg.content);
        break;

      case "tool_call_start": {
        const tc: ToolCallCard = {
          id: msg.tool_call_id || `tc-${++seq}`,
          toolName: msg.tool_name,
          status: "running",
        };
        addToolCall(tc);
        break;
      }

      case "tool_call_args":
        updateLastToolCall({ args: msg.args });
        break;

      case "tool_exec_start":
        // 执行开始 — 状态已是 running，无需额外操作
        break;

      case "tool_exec_end":
        updateLastToolCall({
          status: "done",
          preview: msg.preview,
          truncated: msg.truncated,
          cacheKey: msg.cache_key,
        });
        setLastRunningToolId(null);
        break;

      case "done":
        // 取消时消息保留在屏幕（后端已通过 restore_checkpoint 清理上下文）
        setStreaming(false);
        break;

      case "error":
        setError(msg.message);
        setStreaming(false);
        break;

      case "compact":
        // 上下文压缩通知，静默处理
        break;

      case "pong":
        break;
    }
  }, []);

  const connect = useCallback(() => {
    const protocol = location.protocol === "https:" ? "wss" : "ws";
    const host = location.host || "localhost:8100";
    // 尝试恢复上一次的会话
    const lastSid = localStorage.getItem("lastSessionId");
    const params = lastSid ? `?session_id=${encodeURIComponent(lastSid)}` : "";
    const ws = new WebSocket(`${protocol}://${host}/api/ws/chat${params}`);

    ws.onopen = () => {
      console.log("[ws] connected");
    };

    ws.onmessage = (event) => {
      try {
        const msg: ServerMessage = JSON.parse(event.data);
        handleServerMessage(msg);
      } catch {
        console.warn("[ws] bad message:", event.data);
      }
    };

    ws.onclose = () => {
      console.log("[ws] disconnected");
      // 自动重连（3 秒后）
      reconnectTimer.current = setTimeout(connect, 3000);
    };

    ws.onerror = (err) => {
      console.error("[ws] error:", err);
    };

    wsRef.current = ws;
  }, []);

  const send = useCallback((msg: ClientMessage) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify(msg));
    }
  }, []);

  const stop = useCallback(() => {
    send({ type: "stop" });
    setStreaming(false);
  }, [send, setStreaming]);

  /** 发送聊天消息（含用户输入回填） */
  const sendChat = useCallback(
    (content: string) => {
      setLastUserInput(content);
      send({ type: "chat", content });
    },
    [send, setLastUserInput],
  );

  const disconnect = useCallback(() => {
    if (reconnectTimer.current) clearTimeout(reconnectTimer.current);
    wsRef.current?.close();
    wsRef.current = null;
  }, []);

  // 自动连接
  useEffect(() => {
    connect();
    return () => disconnect();
  }, []);

  return { send, sendChat, stop, disconnect };
}
