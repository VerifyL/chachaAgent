import { useEffect, useState } from "react";
import { useChatStore } from "../store/chatStore";
import { useSessions } from "../hooks/useSessions";

interface Props {
  onNewSession: () => void;
}

export function Sidebar({ onNewSession }: Props) {
  const { sessions, sidebarOpen, toggleSidebar, sessionId } = useChatStore();
  const { fetchSessions, loadHistory, deleteSession } = useSessions();
  const [hoverId, setHoverId] = useState<string | null>(null);

  // 挂载时拉取会话列表
  useEffect(() => {
    fetchSessions();
  }, [fetchSessions]);

  // 移动端检测
  const [isMobile, setIsMobile] = useState(window.innerWidth < 768);
  useEffect(() => {
    const onResize = () => setIsMobile(window.innerWidth < 768);
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  const handleSelectSession = async (id: string) => {
    if (id === sessionId) return;
    await loadHistory(id);
    // 刷新列表以更新 preview
    fetchSessions();
    if (isMobile) toggleSidebar();
  };

  const handleDelete = async (e: React.MouseEvent, sid: string) => {
    e.stopPropagation();
    if (!confirm(`确定删除会话 ${sid.slice(0, 15)}...？`)) return;
    await deleteSession(sid);
  };

  const sidebarContent = (
    <aside
      className="flex flex-col w-72 h-full flex-shrink-0 border-r"
      style={{
        backgroundColor: "var(--bg-secondary)",
        borderColor: "var(--border-color)",
      }}
    >
      {/* 头部 */}
      <div
        className="flex items-center justify-between p-4 border-b"
        style={{ borderColor: "var(--border-color)" }}
      >
        <span className="text-lg font-bold">ChachaAgent</span>
        <button
          onClick={toggleSidebar}
          className="p-1 rounded hover:opacity-70 text-lg"
          title="收起侧边栏"
        >
          ◀
        </button>
      </div>

      {/* 新建对话 */}
      <div className="p-3">
        <button
          onClick={onNewSession}
          className="w-full py-2 px-4 rounded-lg text-sm font-medium transition-colors"
          style={{
            backgroundColor: "var(--accent)",
            color: "#fff",
          }}
        >
          + 新建对话
        </button>
      </div>

      {/* 会话列表 */}
      <div className="flex-1 overflow-y-auto px-2">
        {sessions.length === 0 && (
          <p
            className="text-center text-sm py-8"
            style={{ color: "var(--text-secondary)" }}
          >
            暂无历史会话
          </p>
        )}
        {sessions.map((s) => {
          const isActive = sessionId === s.id;
          return (
            <button
              key={s.id}
              onClick={() => handleSelectSession(s.id)}
              onMouseEnter={() => setHoverId(s.id)}
              onMouseLeave={() => setHoverId(null)}
              className={`group w-full text-left px-3 py-2 rounded-lg mb-1 transition-colors relative border-l-2 ${
                isActive ? "font-medium" : "border-transparent"
              }`}
              style={{
                backgroundColor: isActive
                  ? "var(--active-bg, var(--bg-tertiary))"
                  : "transparent",
                borderLeftColor: isActive
                  ? "var(--accent)"
                  : "transparent",
                color: "var(--text-primary)",
              }}
            >
              <div className="flex items-center gap-2">
                {isActive && (
                  <span
                    className="w-1.5 h-1.5 rounded-full flex-shrink-0"
                    style={{ backgroundColor: "var(--accent)" }}
                  />
                )}
                <div className="truncate text-sm flex-1 min-w-0">
                  {s.preview || `会话 ${s.id.slice(0, 8)}`}
                </div>
              </div>
              <div
                className="text-xs mt-0.5 truncate"
                style={{ color: "var(--text-secondary)" }}
              >
                {s.time}
              </div>
              {/* 删除按钮 */}
              {hoverId === s.id && (
                <span
                  onClick={(e) => handleDelete(e, s.id)}
                  className="absolute right-2 top-1/2 -translate-y-1/2 text-xs px-1.5 py-0.5 rounded hover:bg-red-100 dark:hover:bg-red-900"
                  title="删除会话"
                  style={{ color: "var(--text-secondary)" }}
                >
                  🗑
                </span>
              )}
            </button>
          );
        })}
      </div>

      {/* 底部 */}
      <div
        className="p-3 border-t text-xs"
        style={{
          borderColor: "var(--border-color)",
          color: "var(--text-secondary)",
        }}
      >
        <ThemeToggle />
      </div>
    </aside>
  );

  // 移动端：overlay 模式
  if (isMobile) {
    if (!sidebarOpen) return null;
    return (
      <>
        {/* 遮罩层 */}
        <div
          className="fixed inset-0 z-40 bg-black/40 md:hidden"
          onClick={toggleSidebar}
        />
        {/* 侧边栏浮层 */}
        <div className="fixed left-0 top-0 bottom-0 z-50 shadow-2xl md:hidden">
          {sidebarContent}
        </div>
      </>
    );
  }

  // 桌面端：内联模式
  if (!sidebarOpen) return null;
  return sidebarContent;
}

function ThemeToggle() {
  const { darkMode, toggleDarkMode } = useChatStore();

  return (
    <button
      onClick={toggleDarkMode}
      className="flex items-center gap-2 px-2 py-1 rounded hover:opacity-70"
    >
      <span>{darkMode ? "☀️ 浅色" : "🌙 深色"}</span>
    </button>
  );
}
