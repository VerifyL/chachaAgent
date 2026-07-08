import { useRef, useEffect, useState, type ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import { useChatStore } from "../store/chatStore";
import type { ChatMessage, ToolCallCard } from "../types";

export function MessageList() {
  const { messages, streaming, error, retry, setError } = useChatStore();
  const bottomRef = useRef<HTMLDivElement>(null);

  // 自动滚到底部
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, streaming]);

  return (
    <div className="flex-1 overflow-y-auto px-4 py-6">
      {/* 错误横幅 */}
      {error && <ErrorBanner message={error} onRetry={retry} onDismiss={() => setError(null)} />}

      {messages.length === 0 && <WelcomeScreen />}
      {messages.map((msg, i) => (
        <MessageBubble
          key={msg.id}
          msg={msg}
          isStreaming={streaming && i === messages.length - 1 && msg.role === "assistant"}
          isLastAssistant={msg.role === "assistant" && i === messages.length - 1}
        />
      ))}
      <div ref={bottomRef} />
    </div>
  );
}

// ============================================================
// 错误横幅
// ============================================================
function ErrorBanner({
  message,
  onRetry,
  onDismiss,
}: {
  message: string;
  onRetry: () => void;
  onDismiss: () => void;
}) {
  return (
    <div className="mb-4 p-4 rounded-xl flex items-center gap-3" style={{ backgroundColor: "rgba(239,68,68,0.1)", border: "1px solid rgba(239,68,68,0.3)" }}>
      <span className="text-red-500 text-lg">⚠️</span>
      <div className="flex-1 min-w-0">
        <p className="text-sm font-medium text-red-600 dark:text-red-400">出错了</p>
        <p className="text-xs mt-0.5 truncate" style={{ color: "var(--text-secondary)" }}>
          {message}
        </p>
      </div>
      <button
        onClick={onRetry}
        className="px-3 py-1.5 rounded-lg text-xs font-medium bg-red-500 text-white hover:bg-red-600 transition-colors flex-shrink-0"
      >
        重试
      </button>
      <button
        onClick={onDismiss}
        className="p-1 rounded opacity-50 hover:opacity-100 flex-shrink-0"
        style={{ color: "var(--text-secondary)" }}
      >
        ✕
      </button>
    </div>
  );
}

// ============================================================
// 欢迎屏幕
// ============================================================
function WelcomeScreen() {
  return (
    <div className="flex flex-col items-center justify-center h-full text-center">
      <h1 className="text-4xl font-bold mb-4">ChachaAgent</h1>
      <p className="text-lg mb-2" style={{ color: "var(--text-secondary)" }}>
        你的 AI 编程助手
      </p>
      <p style={{ color: "var(--text-secondary)" }}>
        支持工具调用 · 上下文管理 · MCP 协议
      </p>
      <div className="grid grid-cols-2 gap-3 mt-8 max-w-lg">
        {[
          "帮我解读这段代码",
          "审查当前项目的安全性",
          "写一个 Python 脚本",
          "解释这个错误信息",
        ].map((hint) => (
          <button
            key={hint}
            className="px-4 py-3 rounded-xl text-sm text-left border transition-colors hover:opacity-70"
            style={{
              borderColor: "var(--border-color)",
              backgroundColor: "var(--bg-tertiary)",
            }}
          >
            {hint}
          </button>
        ))}
      </div>
    </div>
  );
}

// ============================================================
// 单条消息气泡
// ============================================================
function MessageBubble({
  msg,
  isStreaming,
  isLastAssistant,
}: {
  msg: ChatMessage;
  isStreaming: boolean;
  isLastAssistant: boolean;
}) {
  const isUser = msg.role === "user";

  return (
    <div className={`mb-6 flex ${isUser ? "justify-end" : "justify-start"}`}>
      <div className={`max-w-3xl w-full ${isUser ? "flex justify-end" : ""}`}>
        {/* 角色标签 */}
        <div className="text-xs font-medium mb-1" style={{ color: "var(--text-secondary)" }}>
          {isUser ? "You" : "Chacha"}
        </div>

        {/* 推理过程 — 首条默认展开，后续折叠 */}
        {msg.reasoning && <ReasoningBlock content={msg.reasoning} autoExpand={isLastAssistant && isStreaming} />}

        {/* 工具调用卡片 */}
        {msg.toolCalls.length > 0 && (
          <div className="mb-2 space-y-1">
            {msg.toolCalls.map((tc) => (
              <ToolCallMini key={tc.id} tc={tc} />
            ))}
          </div>
        )}

        {/* 正文 */}
        {msg.content && (
          <div
            className={`rounded-2xl px-4 py-3 text-sm leading-relaxed ${
              isStreaming ? "streaming-cursor" : ""
            }`}
            style={{
              backgroundColor: isUser ? "var(--accent)" : "var(--bg-tertiary)",
              color: isUser ? "#fff" : "var(--text-primary)",
            }}
          >
            {isUser ? (
              <p className="whitespace-pre-wrap">{msg.content}</p>
            ) : (
              <ReactMarkdown
                remarkPlugins={[remarkGfm]}
                rehypePlugins={[rehypeHighlight]}
                components={{
                  pre: ({ children, ...props }) => (
                    <CodeBlockWrapper {...props}>{children}</CodeBlockWrapper>
                  ),
                }}
              >
                {msg.content}
              </ReactMarkdown>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

// ============================================================
// 代码块包装器（语言标签 + 复制按钮）
// ============================================================
function CodeBlockWrapper({ children, ...props }: { children: ReactNode }) {
  const [copied, setCopied] = useState(false);

  const extractLanguage = (): string | null => {
    const findCodeClass = (node: unknown): string | null => {
      if (!node || typeof node !== "object") return null;
      const n = node as Record<string, unknown>;
      if (n.type === "code" && typeof n.className === "string") {
        const match = n.className.match(/language-(\w+)/);
        return match ? match[1] : null;
      }
      if (n.props && typeof n.props === "object") {
        const p = n.props as Record<string, unknown>;
        if (typeof p.className === "string") {
          const match = p.className.match(/language-(\w+)/);
          return match ? match[1] : null;
        }
        if (Array.isArray(p.children)) {
          for (const child of p.children) {
            const lang = findCodeClass(child);
            if (lang) return lang;
          }
        }
      }
      return null;
    };
    return findCodeClass(children);
  };

  const language = extractLanguage();

  const handleCopy = async () => {
    const extractText = (node: unknown): string => {
      if (!node || typeof node !== "object") return "";
      const n = node as Record<string, unknown>;
      if (typeof n === "string") return n;
      if (typeof n.children === "string") return n.children;
      if (Array.isArray(n.children)) {
        return (n.children as Array<unknown>).map(extractText).join("");
      }
      if (n.props && typeof n.props === "object") {
        const p = n.props as Record<string, unknown>;
        if (typeof p.children === "string") return p.children;
        if (Array.isArray(p.children)) {
          return (p.children as Array<unknown>).map(extractText).join("");
        }
      }
      return "";
    };

    const text = extractText(children);
    await navigator.clipboard.writeText(text);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className="code-block-wrapper">
      <div className="code-block-header">
        <span>{language || "code"}</span>
        <button onClick={handleCopy}>{copied ? "已复制 ✓" : "复制"}</button>
      </div>
      <div className="code-block-body">
        <pre {...props}>{children}</pre>
      </div>
    </div>
  );
}

// ============================================================
// 推理过程折叠块（对标 Claude/DeepSeek 设计）
// ============================================================
function ReasoningBlock({ content, autoExpand }: { content: string; autoExpand: boolean }) {
  const [expanded, setExpanded] = useState(autoExpand);

  // 当流式输出到当前消息时，始终保持展开
  useEffect(() => {
    if (autoExpand) setExpanded(true);
  }, [autoExpand, content]);

  // 估算思考行数
  const lineCount = content.split("\n").length;
  const preview = content.slice(0, 60).replace(/\n/g, " ");

  return (
    <div className="mb-2">
      <button
        onClick={() => setExpanded(!expanded)}
        className="text-xs flex items-center gap-1.5 opacity-60 hover:opacity-100 transition-opacity group"
        style={{ color: "var(--text-secondary)" }}
      >
        <span className="text-[10px] transition-transform" style={{ transform: expanded ? "rotate(90deg)" : "" }}>
          ▶
        </span>
        <span>思考中{autoExpand ? "..." : ` (${lineCount} 行)`}</span>
        {!expanded && !autoExpand && (
          <span className="truncate max-w-40 opacity-40 group-hover:opacity-60">— {preview}…</span>
        )}
      </button>
      {expanded && (
        <div
          className="mt-1 p-3 rounded-lg text-xs leading-relaxed italic border-l-2"
          style={{
            backgroundColor: "var(--bg-secondary)",
            borderColor: "var(--border-color)",
            color: "var(--text-secondary)",
          }}
        >
          {content}
        </div>
      )}
    </div>
  );
}

// ============================================================
// 工具调用迷你卡片
// ============================================================
function ToolCallMini({ tc }: { tc: ToolCallCard }) {
  const [expanded, setExpanded] = useState(false);

  const displayText =
    tc.status === "running"
      ? "执行中..."
      : tc.args
        ? tc.args.slice(0, 80)
        : tc.preview
          ? tc.preview.slice(0, 80)
          : "完成";

  return (
    <div
      className="text-xs rounded-lg border px-3 py-1.5"
      style={{
        backgroundColor: "var(--bg-secondary)",
        borderColor: "var(--border-color)",
      }}
    >
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex items-center gap-1.5 w-full text-left"
      >
        <span>{tc.status === "running" ? "⏳" : "✅"}</span>
        <span className="font-mono text-[11px]" style={{ color: "var(--accent)" }}>
          {tc.toolName}
        </span>
        <span className="truncate opacity-60">— {displayText}</span>
      </button>
      {expanded && (
        <div
          className="mt-2 p-2 rounded font-mono text-[11px] whitespace-pre-wrap max-h-48 overflow-y-auto"
          style={{ backgroundColor: "var(--bg-primary)" }}
        >
          {tc.args && (
            <div className="mb-1">
              <span className="opacity-50">参数: </span>
              {tc.args}
            </div>
          )}
          {tc.preview && (
            <div>
              <span className="opacity-50">结果: </span>
              {tc.preview}
            </div>
          )}
          {tc.truncated && <div className="mt-1 opacity-50 italic">(输出已截断)</div>}
        </div>
      )}
    </div>
  );
}
