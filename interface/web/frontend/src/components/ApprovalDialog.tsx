import { useChatStore } from "../store/chatStore";
import { useWebSocket } from "../hooks/useWebSocket";

/** 风险等级 => 颜色映射 */
function riskColor(level: string): string {
  switch (level) {
    case "critical":
      return "text-red-400";
    case "high":
      return "text-orange-400";
    case "medium":
      return "text-yellow-400";
    default:
      return "text-slate-400";
  }
}

function riskBg(level: string): string {
  switch (level) {
    case "critical":
      return "border-red-500/40 bg-red-500/5";
    case "high":
      return "border-orange-500/40 bg-orange-500/5";
    case "medium":
      return "border-yellow-500/40 bg-yellow-500/5";
    default:
      return "border-slate-500/40 bg-slate-500/5";
  }
}

export function ApprovalDialog() {
  const { sendApprovalResponse } = useWebSocket();
  const pendingApproval = useChatStore((s) => s.pendingApproval);

  if (!pendingApproval) return null;

  const pa = pendingApproval;

  const handleApprove = () => sendApprovalResponse(pa.requestId, true);
  const handleDeny = () => sendApprovalResponse(pa.requestId, false);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm">
      <div
        className={`mx-4 w-full max-w-lg rounded-xl border ${riskBg(pa.riskLevel)} p-0 shadow-2xl`}
      >
        {/* 标题栏 */}
        <div className="flex items-center gap-3 border-b border-slate-700/50 px-5 py-3.5">
          <span className="text-xl">⚠️</span>
          <div className="flex-1">
            <div className="text-sm font-semibold text-slate-200">
              工具执行审批
            </div>
            <div className="text-xs text-slate-400">
              风险等级:
              <span className={`ml-1 font-medium ${riskColor(pa.riskLevel)}`}>
                {pa.riskLevel}
              </span>
              <span className="ml-1 text-slate-500">
                (分数: {Math.round(pa.riskScore)})
              </span>
            </div>
          </div>
        </div>

        {/* 内容区 */}
        <div className="max-h-80 overflow-y-auto px-5 py-3.5 space-y-3 text-sm">
          {/* 工具名 */}
          <div>
            <span className="text-slate-500">工具: </span>
            <code className="rounded bg-slate-800 px-1.5 py-0.5 text-slate-200 font-mono">
              {pa.toolName}
            </code>
          </div>

          {/* 原因 */}
          <div>
            <span className="text-slate-500">原因: </span>
            <span className="text-slate-300">{pa.reason}</span>
          </div>

          {/* 参数 */}
          {Object.keys(pa.arguments).length > 0 && (
            <div>
              <div className="mb-1 text-slate-500">参数:</div>
              <div className="rounded-lg bg-slate-900/60 border border-slate-700/30 p-2.5 font-mono text-xs text-slate-300 max-h-32 overflow-y-auto space-y-1">
                {Object.entries(pa.arguments).map(([k, v]) => (
                  <div key={k} className="flex gap-2">
                    <span className="text-sky-400 shrink-0">{k}:</span>
                    <span className="text-slate-400 break-all">{v}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Diff */}
          {pa.diff && (
            <div>
              <div className="mb-1 text-slate-500">文件变更:</div>
              <pre className="rounded-lg bg-slate-900/60 border border-slate-700/30 p-2.5 font-mono text-xs text-slate-300 max-h-48 overflow-y-auto whitespace-pre-wrap">
                {pa.diff}
              </pre>
            </div>
          )}
        </div>

        {/* 按钮区 */}
        <div className="flex gap-3 border-t border-slate-700/50 px-5 py-3.5">
          <button
            type="button"
            onClick={handleDeny}
            className="flex-1 rounded-lg border border-slate-600 px-4 py-2.5 text-sm text-slate-300 hover:bg-slate-800/50 transition-colors"
          >
            ✕ 拒绝
          </button>
          <button
            type="button"
            onClick={handleApprove}
            className="flex-1 rounded-lg bg-emerald-600 px-4 py-2.5 text-sm text-white hover:bg-emerald-500 transition-colors font-medium"
          >
            ✓ 允许执行
          </button>
        </div>
      </div>
    </div>
  );
}
