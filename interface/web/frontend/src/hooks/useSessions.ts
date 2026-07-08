import { useCallback } from "react";
import { useChatStore } from "../store/chatStore";
import type { SessionSummary, SessionHistory, ChatMessage } from "../types";

const API_BASE = "/api";

let historyMsgSeq = 0;

/**
 * 会话管理 Hook — 对接后端 REST API。
 */
export function useSessions() {
  const { setSessions, setMessages, setSessionId, clearMessages } =
    useChatStore();

  /** 拉取会话列表 */
  const fetchSessions = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/sessions`);
      if (!res.ok) return;
      const data: SessionSummary[] = await res.json();
      setSessions(data);
    } catch (err) {
      console.warn("[sessions] fetch failed:", err);
    }
  }, [setSessions]);

  /** 创建新会话 */
  const createSession = useCallback(async (): Promise<string | null> => {
    try {
      const res = await fetch(`${API_BASE}/sessions`, { method: "POST" });
      if (!res.ok) return null;
      const data = await res.json();
      return data.session_id;
    } catch (err) {
      console.warn("[sessions] create failed:", err);
      return null;
    }
  }, []);

  /** 加载历史消息 */
  const loadHistory = useCallback(
    async (sessionId: string) => {
      try {
        const res = await fetch(`${API_BASE}/sessions/${sessionId}`);
        if (!res.ok) return;
        const data: SessionHistory = await res.json();

        const messages: ChatMessage[] = data.messages.map((m) => ({
          id: `hist-${++historyMsgSeq}`,
          role: m.role,
          content: m.content,
          toolCalls: [],
          timestamp: Date.now(),
        }));

        setMessages(messages);
        setSessionId(sessionId);
      } catch (err) {
        console.warn("[sessions] load history failed:", err);
      }
    },
    [setMessages, setSessionId],
  );

  /** 删除会话 */
  const deleteSession = useCallback(
    async (sessionId: string) => {
      try {
        const res = await fetch(`${API_BASE}/sessions/${sessionId}`, {
          method: "DELETE",
        });
        if (!res.ok) return;
        // 如果删除的是当前会话，清空消息
        const { sessionId: currentId } = useChatStore.getState();
        if (currentId === sessionId) {
          clearMessages();
        }
        // 刷新列表
        await fetchSessions();
      } catch (err) {
        console.warn("[sessions] delete failed:", err);
      }
    },
    [fetchSessions, clearMessages],
  );

  /** 新建对话（清空当前 + 获取新 session_id） */
  const newSession = useCallback(async () => {
    clearMessages();
    const newId = await createSession();
    if (newId) {
      setSessionId(newId);
    }
    await fetchSessions();
  }, [clearMessages, createSession, setSessionId, fetchSessions]);

  return { fetchSessions, createSession, loadHistory, deleteSession, newSession };
}
