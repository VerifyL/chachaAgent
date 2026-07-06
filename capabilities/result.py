"""
ToolResult — 所有工具返回的统一结果结构。

设计原则:
  - LLM-facing 字段: 序列化为 JSON 返回给 LLM，帮助其理解结果并决策下一步
  - Internal-only 字段: 序列化时排除，供 debug / telemetry / trace 消费
  - 8 个工具共用同一个结构，通过 data / internal 承载各自差异
"""

from typing import Literal

from pydantic import BaseModel, Field


class ToolResult(BaseModel):
    """所有工具返回的统一结果。

    序列化行为:
        model_dump()          → 仅 LLM-facing 字段
        model_dump(exclude_none=True) → LLM-facing 字段，去掉 None
        model_dump(include={"tool_name","execution_time_ms","trace","internal"})
                              → internal-only 字段（调试/追踪用）
    """

    # ═══════════════════════════════════════════════════════════
    # LLM-facing — 序列化给 LLM，帮助理解结果 & 决策下一步
    # ═══════════════════════════════════════════════════════════

    status: Literal["success", "error"]
    """success=正常完成 / error=执行失败"""

    content: str
    """主体内容: read=代码文本, edit=变更摘要, grep=匹配结果, ..."""

    error: str | None = None
    """人类可读的错误描述 (status=error 时填充)"""

    error_type: str | None = None
    """机器可读错误分类，帮助 LLM 选择恢复策略。
    通用枚举: file_not_found | permission_denied | timeout |
             parse_error | exit_code_nonzero | invalid_argument |
             network_error | unknown
    """

    truncated: bool = False
    """输出是否被截断。True 时 LLM 应使用 cache_key 续读"""

    truncated_from: int | None = None
    """截断前的原始字符数，给 LLM 评估剩余内容量"""

    cache_key: str | None = None
    """截断缓存 key，LLM 凭此通过 cache_read (Tier 3) 续读完整输出"""

    data: dict = Field(default_factory=dict)
    """工具级结构化元数据，LLM 可用于进一步计算或判断:
      read → {path, offset, limit, total_lines, encoding}
      grep → {matches, files, mode}
      bash → {command, exit_code}
      edit → {path, replacements, bytes_written}
      write → {path, lines, bytes}
      glob → {pattern, count, max_depth}
      task  → {subagent_id, subagent_type}
      memory → {topic, action}
    """

    warnings: list[str] = Field(default_factory=list)
    """非致命警告，LLM 可决定是否告知用户。
    例: ["文件较大 (3.2MB)，建议用 offset 分页读取",
         "stderr: main.c:3:10: fatal error: 'missing.h' not found"]"""

    # ═══════════════════════════════════════════════════════════
    # Internal-only — 序列化时排除，内部消费
    # ═══════════════════════════════════════════════════════════

    tool_name: str | None = Field(default=None, exclude=True)
    """工具名 (read/edit/write/...)，日志 & 统计用"""

    execution_time_ms: int | None = Field(default=None, exclude=True)
    """执行耗时 (毫秒)，性能统计 & telemetry"""

    trace: dict = Field(default_factory=dict, exclude=True)
    """分布式追踪上下文:
      {span_id, parent_span_id, tool_call_id, trace_id}
    """

    internal: dict = Field(default_factory=dict, exclude=True)
    """内部调试信息，LLM 绝不需要:
      异常栈、沙箱状态、资源用量、重试次数、原始返回值等
      例: {raw_exception, sandbox_warning, retry_count, peak_memory_mb}
    """
