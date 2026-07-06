"""
core/models/hook.py
钩子上下文与结果模型：不可变上下文在责任链中传递，HookResult 禁止副作用。

设计原则：
1. HookContext 不可变（frozen），在钩子链中只读传递
2. HookResult 是纯返回值，钩子不直接修改全局状态
3. additional_context 允许钩子向对话注入额外消息（系统提示/警告）
4. HookMatcher 决定哪些钩子在哪些事件上触发，支持工具名/命令/组合匹配
5. 钩子链由 HookOrchestrator 驱动，本模块仅定义数据契约
"""

import re
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

# ========================= 1. 钩子挂载点 =========================


class HookPoint(str, Enum):
    """钩子在编排生命周期中的挂载点"""

    PRE_TOOL_EXECUTION = "pre_tool_execution"
    POST_TOOL_EXECUTION = "post_tool_execution"
    PRE_LLM_CALL = "pre_llm_call"
    POST_LLM_CALL = "post_llm_call"
    PRE_CONTEXT_ASSEMBLY = "pre_context_assembly"
    POST_CONTEXT_ASSEMBLY = "post_context_assembly"
    ON_SESSION_START = "on_session_start"
    ON_SESSION_END = "on_session_end"
    ON_ERROR = "on_error"
    PRE_SUBAGENT_SPAWN = "pre_subagent_spawn"  # 子Agent 孵化前
    POST_SUBAGENT_SPAWN = "post_subagent_spawn"  # 子Agent 孵化后


# ========================= 2. 钩子决策动作 =========================


class HookAction(str, Enum):
    """钩子返回的决策动作"""

    CONTINUE = "continue"  # 传递到下一个钩子
    STOP = "stop"  # 停止链，继续原始操作（短路但不拒绝）
    BLOCK = "block"  # 拒绝当前操作（安全拦截）
    MODIFY = "modify"  # 修改数据后继续传递


# ========================= 3. 钩子匹配器 =========================


class HookMatcher(BaseModel):
    """决定哪些钩子在哪些事件上触发。

    用法:
        # 匹配所有读/写文件工具
        HookMatcher(type="tool_name", pattern="read|write")

        # 匹配包含 "git push" 的命令
        HookMatcher(type="command", pattern="git\\s+push")

        # 复合条件：工具名为 "shell" 且命令包含 "pip"
        HookMatcher(
            type="composite",
            composite_op="and",
            children=[
                HookMatcher(type="tool_name", pattern="shell"),
                HookMatcher(type="command", pattern="pip"),
            ]
        )

        # 取反：匹配所有非 "shell" 的工具
        HookMatcher(type="tool_name", pattern="shell", invert=True)
    """

    model_config = ConfigDict(frozen=True, use_enum_values=True)

    type: str = Field(default="always", description="匹配类型：always | tool_name | command | composite")
    pattern: Optional[str] = Field(None, description="正则表达式，用于 tool_name / command 类型")
    invert: bool = Field(False, description="是否取反匹配")
    composite_op: Optional[str] = Field(None, description="组合操作符：and | or，仅在 type=composite 时有效")
    children: Optional[List["HookMatcher"]] = Field(None, description="子匹配器列表，仅在 type=composite 时有效")

    def matches(self, tool_name: Optional[str] = None, command: Optional[str] = None) -> bool:
        """判断当前工具/命令是否匹配。

        由 HookOrchestrator 调用，不在模型中自动执行。
        """
        if self.type == "always":
            return True

        if self.type == "tool_name":
            result = self._regex_match(self.pattern, tool_name)
            return not result if self.invert else result

        if self.type == "command":
            result = self._regex_match(self.pattern, command)
            return not result if self.invert else result

        if self.type == "composite" and self.children:
            results = [child.matches(tool_name, command) for child in self.children]
            if self.composite_op == "and":
                return all(results)
            else:  # "or"
                return any(results)

        return True

    @staticmethod
    def _regex_match(pattern: Optional[str], value: Optional[str]) -> bool:
        if pattern is None or value is None:
            return False
        try:
            return bool(re.search(pattern, value))
        except re.error:
            return False


# ========================= 4. 上下文子结构 =========================


class ToolCallContext(BaseModel):
    """工具调用上下文（PRE/POST_TOOL_EXECUTION 时填充）"""

    model_config = ConfigDict(frozen=True)

    tool_name: str = Field(..., description="工具名称")
    tool_use_id: str = Field(..., description="工具调用唯一 ID")
    arguments: Dict[str, Any] = Field(default_factory=dict, description="工具参数（可能已脱敏）")
    command_or_action: Optional[str] = Field(None, description="执行的命令或操作字符串")


class LLMRequestContext(BaseModel):
    """LLM 请求上下文（PRE/POST_LLM_CALL 时填充）"""

    model_config = ConfigDict(frozen=True)

    model_name: str = Field(..., description="模型名称")
    provider: str = Field(..., description="模型提供商")
    messages_count: int = Field(0, ge=0, description="消息数量")
    estimated_input_tokens: int = Field(0, ge=0, description="预估输入 token 数")


class ErrorContext(BaseModel):
    """错误上下文（ON_ERROR 时填充）"""

    model_config = ConfigDict(frozen=True)

    exception_type: str = Field(..., description="异常类型名")
    message: str = Field(..., description="异常消息")
    source_module: Optional[str] = Field(None, description="异常来源模块")
    recoverable: bool = Field(False, description="是否可恢复")


# ========================= 5. 钩子上下文 =========================


class HookContext(BaseModel):
    """钩子不可变上下文，在责任链中传递。

    根据 hook_point 不同，相应的子上下文字段可能会填充：
      - PRE/POST_TOOL_EXECUTION → tool_call
      - PRE/POST_LLM_CALL       → llm_request
      - ON_ERROR                → error
    """

    model_config = ConfigDict(frozen=True, use_enum_values=True)

    id: str = Field(default_factory=lambda: str(uuid4()), description="上下文唯一 ID")
    hook_point: HookPoint = Field(..., description="当前挂载点")

    # 会话标识
    session_id: Optional[str] = Field(None, description="会话 ID")
    project_id: Optional[str] = Field(None, description="项目 ID")

    # 按挂载点填充的子上下文
    tool_call: Optional[ToolCallContext] = Field(None, description="工具调用上下文")
    llm_request: Optional[LLMRequestContext] = Field(None, description="LLM 请求上下文")
    error: Optional[ErrorContext] = Field(None, description="错误上下文")

    # 匹配器信息（由 HookOrchestrator 填充）
    matched_by: Optional[HookMatcher] = Field(None, description="触发此钩子的匹配器")

    # 扩展元数据（只读）
    metadata: Dict[str, Any] = Field(
        default_factory=dict,
        description="自定义扩展元数据",
        frozen=True,
    )


# ========================= 6. 钩子结果 =========================


class HookResult(BaseModel):
    """钩子返回值，纯数据，禁止副作用。

    设计决策：
      - action: 钩子的决策（CONTINUE/STOP/BLOCK/MODIFY）
      - message: 给开发者/调试看的说明
      - modified_tool_args: MODIFY 时修改后的工具参数
      - additional_context: 注入 LLM 对话的消息（如警告/提示）
      - metadata: 钩子自定义透传数据
    """

    model_config = ConfigDict(frozen=True, use_enum_values=True)

    action: HookAction = Field(default=HookAction.CONTINUE, description="钩子决策")
    message: Optional[str] = Field(None, description="决策说明（开发者日志）")
    modified_tool_args: Optional[Dict[str, Any]] = Field(
        None, description="修改后的工具参数（仅在 action=MODIFY 时有效）"
    )
    additional_context: Optional[str] = Field(None, description="注入对话的消息（系统提示/警告/上下文补充）")
    metadata: Dict[str, Any] = Field(
        default_factory=dict,
        description="钩子自定义透传数据",
    )

    # ---------- 便捷方法 ----------

    def is_continue(self) -> bool:
        return self.action == HookAction.CONTINUE

    def is_blocked(self) -> bool:
        return self.action == HookAction.BLOCK

    def is_modified(self) -> bool:
        return self.action == HookAction.MODIFY

    def is_stopped(self) -> bool:
        return self.action == HookAction.STOP

    @classmethod
    def continue_(cls, message: Optional[str] = None, additional_context: Optional[str] = None) -> "HookResult":
        """工厂：继续传递"""
        return cls(
            action=HookAction.CONTINUE,
            message=message,
            additional_context=additional_context,
        )

    @classmethod
    def block(cls, message: str, additional_context: Optional[str] = None) -> "HookResult":
        """工厂：拒绝操作"""
        return cls(
            action=HookAction.BLOCK,
            message=message,
            additional_context=additional_context,
        )

    @classmethod
    def modify(
        cls,
        modified_tool_args: Dict[str, Any],
        message: Optional[str] = None,
        additional_context: Optional[str] = None,
    ) -> "HookResult":
        """工厂：修改参数后继续"""
        return cls(
            action=HookAction.MODIFY,
            message=message,
            modified_tool_args=modified_tool_args,
            additional_context=additional_context,
        )

    @classmethod
    def stop(cls, message: Optional[str] = None) -> "HookResult":
        """工厂：停止责任链但不拒绝"""
        return cls(
            action=HookAction.STOP,
            message=message,
        )
