import { useEffect, useRef, useCallback } from "react";
import { useChatStore } from "../store/chatStore";
import type { ServerMessage, ClientMessage, PendingApproval, ToolCallCard } from "../types";

// ── 模块级单例：所有 useWebSocket() 调用共享同一个连接 ──
let seq = 0;
let _ws: WebSocket | null = null;
let _reconnectTimer: ReturnType<typeof setTimeout> | undefined;

function getWs(): WebSocket | null {
  return _ws;
}

function setWs(ws: WebSocket | null) {
  _ws = ws;
}

function setReconnectTimer(t: ReturnType<typeof setTimeout> | undefined) {
  if (_reconnectTimer) clearTimeout(_reconnectTimer);
  _reconnectTimer = t;
}

/** 模块级 send：无需依赖 React ref */
function sendRaw(msg: ClientMessage) {
  const ws = getWs();
  if (ws?.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(msg));
  }
}

export function useWebSocket() {
  // 用 ref 跟踪当前组件是否已初始化连接
  const initiated = useRef(false);

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
    showToast,
    setPendingApproval,
  } = useChatStore();

  const handleServerMessage = useCallback((msg: ServerMessage) => {
    switch (msg.type) {
      case "session_created": {
        setSessionId(msg.session_id);
        const lastSid = localStorage.getItem("lastSessionId");
        const isRestore = lastSid === msg.session_id;
        localStorage.setItem("lastSessionId", msg.session_id);

        if (!isRestore) {
          addSession({
            id: msg.session_id,
            preview: "（新对话）",
            time: new Date().toLocaleString(),
          });
        }
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
        updateLastToolCall({ args: msg.args });
        break;

      case "tool_exec_end":
        updateLastToolCall({
          status: "done",
          preview: msg.preview,
          diff: msg.diff,
          truncated: msg.truncated,
          cacheKey: msg.cache_key,
        });
        setLastRunningToolId(null);
        break;

      case "done":
        setStreaming(false);
        break;

      case "error":
        setError(msg.message);
        setStreaming(false);
        break;

      case "compact":
        {
          const oldK = Math.round(msg.old_tokens / 1000);
          const newK = Math.round(msg.new_tokens / 1000);
          const saved = oldK - newK;
          const ratio = oldK > 0 ? Math.round((saved / oldK) * 100) : 0;
          if (msg.old_msgs === 0 && msg.new_msgs === 0) {
            showToast("✅ 上下文已是最小，无需压缩");
          } else {
            showToast(
              `✅ 压缩完成：消息 ${msg.old_msgs}→${msg.new_msgs}，token ${oldK}K→${newK}K（-${ratio}%）`,
            );
          }
        }
        break;

      case "permission_request": {
        const pa: PendingApproval = {
          requestId: msg.request_id,
          toolName: msg.tool_name,
          arguments: msg.arguments,
          riskLevel: msg.risk_level,
          riskScore: msg.risk_score,
          reason: msg.reason,
          diff: msg.diff,
        };
        setPendingApproval(pa);
        break;
      }

      case "pong":
        break;
    }
  }, [appendContent, appendReasoning, addToolCall, updateLastToolCall, setLastRunningToolId, setStreaming, setSessionId, setError, setLastUserInput, addSession, showToast, setPendingApproval]);

  const connect = useCallback(() => {
    // 如果已有连接，不重复创建
    if (getWs()?.readyState === WebSocket.OPEN || getWs()?.readyState === WebSocket.CONNECTING) {
      return;
    }

    const protocol = location.protocol === "https:" ? "wss" : "ws";
    const host = location.host || "localhost:8100";
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
      setWs(null);
      // 自动重连（3 秒后）
      setReconnectTimer(setTimeout(connect, 3000));
    };

    ws.onerror = (err) => {
      console.error("[ws] error:", err);
    };

    setWs(ws);
  }, [handleServerMessage]);

  const send = useCallback((msg: ClientMessage) => {
    sendRaw(msg);
  }, []);

  const stop = useCallback(() => {
    send({ type: "stop" });
    setStreaming(false);
  }, [send, setStreaming]);

  const sendChat = useCallback(
    (content: string) => {
      setLastUserInput(content);
      send({ type: "chat", content });
    },
    [send, setLastUserInput],
  );

  const sendCompact = useCallback(() => {
    send({ type: "compact_now" });
  }, [send]);

  const sendApprovalResponse = useCallback(
    (requestId: string, approved: boolean) => {
      send({ type: "permission_response", request_id: requestId, approved });
      setPendingApproval(null);
    },
    [send, setPendingApproval],
  );

  const disconnect = useCallback(() => {
    setReconnectTimer(undefined);
    getWs()?.close();
    setWs(null);
  }, []);

  // 自动连接（仅首次调用时执行）
  useEffect(() => {
    if (!initiated.current) {
      initiated.current = true;
      connect();
    }
    return () => {
      // 不在此处 disconnect，让连接在组件卸载后依然存活
    };
  }, [connect]);

  return { send, sendChat, sendCompact, sendApprovalResponse, stop, disconnect };
}
