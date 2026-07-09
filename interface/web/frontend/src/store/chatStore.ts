import { create } from "zustand";
import type { ChatMessage, SessionSummary, ToolCallCard } from "../types";

interface ChatState {
  // 会话
  sessionId: string | null;
  sessions: SessionSummary[];
  /** 按 sessionId 分组的消息，切换会话不丢失 */
  messagesBySession: Record<string, ChatMessage[]>;
  loading: boolean;
  streaming: boolean;

  // 副作用
  error: string | null;
  darkMode: boolean;
  sidebarOpen: boolean;
  lastRunningToolId: string | null;
  lastUserInput: string | null; // 用于错误后重试

  // actions
  setSessionId: (id: string | null) => void;
  setSessions: (sessions: SessionSummary[]) => void;
  addSession: (session: SessionSummary) => void;
  addMessage: (msg: ChatMessage) => void;
  appendContent: (content: string) => void;
  appendReasoning: (content: string) => void;
  addToolCall: (tc: ToolCallCard) => void;
  updateToolCall: (id: string, update: Partial<ToolCallCard>) => void;
  updateLastToolCall: (update: Partial<ToolCallCard>) => void;
  setLastRunningToolId: (id: string | null) => void;
  setLoading: (v: boolean) => void;
  setStreaming: (v: boolean) => void;
  setError: (e: string | null) => void;
  toggleDarkMode: () => void;
  toggleSidebar: () => void;
  setLastUserInput: (v: string | null) => void;
  setMessages: (msgs: ChatMessage[]) => void;
  clearMessages: () => void;
  retry: () => void; // 占位，由 App 层注入
  setRetry: (fn: () => void) => void;
}

/** 从分组中获取当前会话的消息 */
function getCurrentMessages(s: ChatState): ChatMessage[] {
  return s.sessionId ? (s.messagesBySession[s.sessionId] ?? []) : [];
}

export const useChatStore = create<ChatState>((set) => ({
  sessionId: null,
  sessions: [],
  messagesBySession: {},
  loading: false,
  streaming: false,
  error: null,
  darkMode: window.matchMedia("(prefers-color-scheme: dark)").matches,
  sidebarOpen: true,
  lastRunningToolId: null,
  lastUserInput: null,

  setSessionId: (id) => set({ sessionId: id }),
  setSessions: (sessions) => set({ sessions }),
  addSession: (session) =>
    set((s) => {
      if (s.sessions.some((x) => x.id === session.id)) return s;
      return { sessions: [session, ...s.sessions] };
    }),
  addMessage: (msg) =>
    set((s) => {
      if (!s.sessionId) return { messagesBySession: s.messagesBySession };
      const msgs = [...(s.messagesBySession[s.sessionId] ?? []), msg];
      return {
        messagesBySession: { ...s.messagesBySession, [s.sessionId]: msgs },
        error: null,
      };
    }),
  appendContent: (content) =>
    set((s) => {
      if (!s.sessionId) return s;
      const msgs = [...(s.messagesBySession[s.sessionId] ?? [])];
      const last = msgs[msgs.length - 1];
      if (last && last.role === "assistant") {
        msgs[msgs.length - 1] = {
          ...last,
          content: last.content + content,
        };
      }
      return {
        messagesBySession: { ...s.messagesBySession, [s.sessionId!]: msgs },
      };
    }),
  appendReasoning: (content) =>
    set((s) => {
      if (!s.sessionId) return s;
      const msgs = [...(s.messagesBySession[s.sessionId] ?? [])];
      const last = msgs[msgs.length - 1];
      if (last && last.role === "assistant") {
        msgs[msgs.length - 1] = {
          ...last,
          reasoning: (last.reasoning || "") + content,
        };
      }
      return {
        messagesBySession: { ...s.messagesBySession, [s.sessionId!]: msgs },
      };
    }),
  addToolCall: (tc) =>
    set((s) => {
      if (!s.sessionId) return s;
      const msgs = [...(s.messagesBySession[s.sessionId] ?? [])];
      const last = msgs[msgs.length - 1];
      if (last && last.role === "assistant") {
        msgs[msgs.length - 1] = {
          ...last,
          toolCalls: [...last.toolCalls, tc],
        };
      }
      return {
        messagesBySession: { ...s.messagesBySession, [s.sessionId!]: msgs },
        lastRunningToolId: tc.id,
      };
    }),
  updateToolCall: (id, update) =>
    set((s) => {
      if (!s.sessionId) return s;
      const msgs = [...(s.messagesBySession[s.sessionId] ?? [])];
      const last = msgs[msgs.length - 1];
      if (last && last.role === "assistant") {
        msgs[msgs.length - 1] = {
          ...last,
          toolCalls: last.toolCalls.map((tc) =>
            tc.id === id ? { ...tc, ...update } : tc,
          ),
        };
      }
      return {
        messagesBySession: { ...s.messagesBySession, [s.sessionId!]: msgs },
      };
    }),
  updateLastToolCall: (update) =>
    set((s) => {
      if (!s.lastRunningToolId || !s.sessionId) return s;
      const msgs = [...(s.messagesBySession[s.sessionId] ?? [])];
      const last = msgs[msgs.length - 1];
      if (last && last.role === "assistant") {
        msgs[msgs.length - 1] = {
          ...last,
          toolCalls: last.toolCalls.map((tc) =>
            tc.id === s.lastRunningToolId ? { ...tc, ...update } : tc,
          ),
        };
      }
      return {
        messagesBySession: { ...s.messagesBySession, [s.sessionId!]: msgs },
      };
    }),
  setLastRunningToolId: (id) => set({ lastRunningToolId: id }),
  setLoading: (v) => set({ loading: v }),
  setStreaming: (v) => set({ streaming: v }),
  setError: (e) => set({ error: e }),
  setLastUserInput: (v) => set({ lastUserInput: v }),
  retry: () => {},
  setRetry: (fn) => set({ retry: fn }),
  toggleDarkMode: () =>
    set((s) => ({ darkMode: !s.darkMode })),
  toggleSidebar: () =>
    set((s) => ({ sidebarOpen: !s.sidebarOpen })),
  setMessages: (msgs) =>
    set((s) => {
      if (!s.sessionId) return { messagesBySession: s.messagesBySession };
      return {
        messagesBySession: { ...s.messagesBySession, [s.sessionId]: msgs },
      };
    }),
  clearMessages: () =>
    set((s) => {
      if (!s.sessionId) return { messagesBySession: s.messagesBySession, sessionId: null, lastRunningToolId: null };
      const { [s.sessionId]: _, ...rest } = s.messagesBySession;
      return { messagesBySession: rest, sessionId: null, lastRunningToolId: null };
    }),
}));

/** 获取当前会话的消息列表（用于组件） */
export function useCurrentMessages(): ChatMessage[] {
  return useChatStore((s) => getCurrentMessages(s));
}
