import { create } from "zustand";
import type { ChatMessage, SessionSummary, ToolCallCard } from "../types";

interface ChatState {
  // 会话
  sessionId: string | null;
  sessions: SessionSummary[];
  messages: ChatMessage[];
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

export const useChatStore = create<ChatState>((set) => ({
  sessionId: null,
  sessions: [],
  messages: [],
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
    set((s) => ({ messages: [...s.messages, msg], error: null })),
  appendContent: (content) =>
    set((s) => {
      const msgs = [...s.messages];
      const last = msgs[msgs.length - 1];
      if (last && last.role === "assistant") {
        msgs[msgs.length - 1] = {
          ...last,
          content: last.content + content,
        };
      }
      return { messages: msgs };
    }),
  appendReasoning: (content) =>
    set((s) => {
      const msgs = [...s.messages];
      const last = msgs[msgs.length - 1];
      if (last && last.role === "assistant") {
        msgs[msgs.length - 1] = {
          ...last,
          reasoning: (last.reasoning || "") + content,
        };
      }
      return { messages: msgs };
    }),
  addToolCall: (tc) =>
    set((s) => {
      const msgs = [...s.messages];
      const last = msgs[msgs.length - 1];
      if (last && last.role === "assistant") {
        msgs[msgs.length - 1] = {
          ...last,
          toolCalls: [...last.toolCalls, tc],
        };
      }
      return { messages: msgs, lastRunningToolId: tc.id };
    }),
  updateToolCall: (id, update) =>
    set((s) => {
      const msgs = [...s.messages];
      const last = msgs[msgs.length - 1];
      if (last && last.role === "assistant") {
        msgs[msgs.length - 1] = {
          ...last,
          toolCalls: last.toolCalls.map((tc) =>
            tc.id === id ? { ...tc, ...update } : tc,
          ),
        };
      }
      return { messages: msgs };
    }),
  updateLastToolCall: (update) =>
    set((s) => {
      if (!s.lastRunningToolId) return s;
      const msgs = [...s.messages];
      const last = msgs[msgs.length - 1];
      if (last && last.role === "assistant") {
        msgs[msgs.length - 1] = {
          ...last,
          toolCalls: last.toolCalls.map((tc) =>
            tc.id === s.lastRunningToolId ? { ...tc, ...update } : tc,
          ),
        };
      }
      return { messages: msgs };
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
  setMessages: (msgs) => set({ messages: msgs }),
  clearMessages: () => set({ messages: [], sessionId: null, lastRunningToolId: null }),
}));
