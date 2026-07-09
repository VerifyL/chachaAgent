// ============================================================
// WebSocket 消息类型 — 与服务端 StreamEvent 对齐
// ============================================================

// 服务端 → 客户端
export type ServerMessage =
  | { type: "text"; content: string }
  | { type: "reasoning"; content: string }
  | { type: "tool_call_start"; tool_name: string; tool_call_id?: string }
  | { type: "tool_call_args"; tool_name: string; args: string }
  | { type: "tool_exec_start"; tool_name: string; preview: string }
  | { type: "tool_exec_end"; tool_name: string; preview: string; truncated?: boolean; cache_key?: string }
  | { type: "done"; tokens: number; cancelled?: boolean }
  | { type: "error"; message: string }
  | { type: "compact"; summary: string }
  | { type: "session_created"; session_id: string }
  | { type: "pong" };

// 客户端 → 服务端
export type ClientMessage =
  | { type: "chat"; content: string }
  | { type: "new_session" }
  | { type: "stop" }
  | { type: "ping" };

// 对话中的消息气泡
export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  reasoning?: string;
  toolCalls: ToolCallCard[];
  timestamp: number;
}

export interface ToolCallCard {
  id: string;
  toolName: string;
  args?: string;
  preview?: string;
  status: "running" | "done";
  truncated?: boolean;
  cacheKey?: string;
}

// 会话摘要（对齐 GET /api/sessions 返回）
export interface SessionSummary {
  id: string;
  preview: string;
  time: string;
}

// 历史消息（对齐 GET /api/sessions/{id} 返回）
export interface SessionHistory {
  session_id: string;
  messages: { role: "user" | "assistant"; content: string }[];
  days: string[];
}
