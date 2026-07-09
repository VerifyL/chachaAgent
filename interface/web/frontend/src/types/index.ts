// ============================================================
// WebSocket 消息类型 — 与服务端 StreamEvent 对齐
// ============================================================

// 服务端 → 客户端
export type ServerMessage =
  | { type: "text"; content: string }
  | { type: "reasoning"; content: string }
  | { type: "tool_call_start"; tool_name: string; tool_call_id?: string }
  | { type: "tool_call_args"; tool_name: string; args: string }
  | { type: "tool_exec_start"; tool_name: string; args: string }
  | { type: "tool_exec_end"; tool_name: string; preview: string; diff?: string; truncated?: boolean; cache_key?: string }
  | { type: "permission_request"; request_id: string; tool_name: string; arguments: Record<string, string>; risk_level: string; risk_score: number; reason: string; diff?: string | null }
  | { type: "done"; tokens: number; cancelled?: boolean }
  | { type: "error"; message: string }
  | { type: "compact"; reason: string; old_msgs: number; new_msgs: number; old_tokens: number; new_tokens: number }
  | { type: "session_created"; session_id: string }
  | { type: "pong" };

// 客户端 → 服务端
export type ClientMessage =
  | { type: "chat"; content: string }
  | { type: "new_session" }
  | { type: "stop" }
  | { type: "compact_now" }
  | { type: "permission_response"; request_id: string; approved: boolean }
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
  diff?: string;
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

// 审批请求（前端展示用）
export interface PendingApproval {
  requestId: string;
  toolName: string;
  arguments: Record<string, string>;
  riskLevel: string;
  riskScore: number;
  reason: string;
  diff?: string | null;
}
export interface SessionHistory {
  session_id: string;
  messages: { role: "user" | "assistant"; content: string }[];
  days: string[];
}
