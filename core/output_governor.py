"""
core/output_governor.py
OutputGovernor — 流式 JSON 修复器 + 非法内容拦截器。

设计理念（融合 Harness 四步防线 + 结构化块）：
1. 流式 JSON 修复：LLM tool_calls 增量片段可能残缺（缺闭合括号、引号、逗号），
   通过状态机追踪深度，在流结束时自动补全。
2. LLM 自愈策略：机械修复全部失败时，返回 needs_llm_fix 标志，
   Orchestrator 可将残缺 JSON 发回 LLM 修复。
3. 非法内容拦截：正则 + 关键字匹配，拦截敏感/有害输出（配合 PolicyEngine）。
4. 块类型识别：区分 TextBlock / ToolUseBlock / ThinkingBlock，文本透传、工具缓冲、思考透传。
5. 修复置信度：高（补括号）→ 中（截断）→ 低（补引号）→ 失败，低置信度触发 LLM 自愈。

用法:
    gov = OutputGovernor()
    for chunk in llm_stream:
        text = gov.feed(chunk)
        if text is not None:
            yield text
    final, fixed, needs_fix = gov.flush()  # 修复 + 置信度 + 是否需要 LLM 自愈
"""

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Tuple


# ========================= 块类型与修复置信度 =========================

class BlockType(str, Enum):
    """流式内容块类型（参考 Claude Code ContentBlock）"""
    TEXT = "text"          # 纯文本输出 → 透传
    TOOL_USE = "tool_use"  # 工具调用 JSON → 缓冲累积后修复
    THINKING = "thinking"  # 思考过程 → 透传（不缓冲）


class RepairConfidence(str, Enum):
    """JSON 修复置信度"""
    HIGH = "high"        # 仅补括号 → 几乎确定正确
    MEDIUM = "medium"    # 截断/去尾逗号 → 可能丢失了部分参数
    LOW = "low"          # 补引号/其他 → 修复不精确
    FAILED = "failed"    # 完全不可修复 → 需 LLM 自愈


@dataclass
class FlushResult:
    """flush() 返回的完整结果"""
    output: str                           # 修复后的文本
    repaired: bool                        # 是否进行了修复
    confidence: RepairConfidence          # 修复置信度
    needs_llm_fix: bool                   # 是否需要 LLM 自愈


# ========================= 非法内容规则 =========================

@dataclass
class ContentRule:
    """非法内容匹配规则"""
    pattern: str                           # 正则表达式
    description: str                       # 规则说明
    severity: str = "block"                # block | warn | sanitize


# 默认规则集（可扩展）
DEFAULT_CONTENT_RULES = [
    ContentRule(r"(?i)\brm\s+-rf\s+/", "危险命令 rm -rf /"),
    ContentRule(r"(?i)\bsudo\b", "特权命令 sudo"),
    ContentRule(
        r"(?i)(api[_-]?key|access[_-]?token|secret[_-]?key)[\s:=]+['\"]?[\w-]{20,}['\"]?",
        "疑似泄露 API Key/Token",
        severity="sanitize",
    ),
]


# ========================= JSON 修复器 =========================

@dataclass
class JSONRepairState:
    """JSON 修复状态机"""
    depth: int = 0                         # {} 嵌套深度
    array_depth: int = 0                   # [] 嵌套深度  
    in_string: bool = False                # 是否在字符串内
    escape_next: bool = False              # 下一个字符是否被转义
    buffer: List[str] = field(default_factory=list)

    def reset(self) -> None:
        self.depth = 0
        self.array_depth = 0
        self.in_string = False
        self.escape_next = False

    def feed(self, char: str) -> None:
        """喂入单个字符，更新状态"""
        self.buffer.append(char)

        if self.escape_next:
            self.escape_next = False
            return

        if char == '\\' and self.in_string:
            self.escape_next = True
            return

        if char == '"':
            self.in_string = not self.in_string
            return

        if self.in_string:
            return

        if char == '{':
            self.depth += 1
        elif char == '}':
            self.depth -= 1
        elif char == '[':
            self.array_depth += 1
        elif char == ']':
            self.array_depth -= 1

    @property
    def balanced(self) -> bool:
        """JSON 是否平衡（所有括号闭合、不在字符串中）"""
        return self.depth == 0 and self.array_depth == 0 and not self.in_string


# ========================= 输出治理器 =========================

class OutputGovernor:
    """
    流式输出治理器。

    负责：块类型识别、JSON 修复、非法内容拦截、LLM 自愈触发。

    用法:
        gov = OutputGovernor()
        for chunk in llm_stream:
            text = gov.feed(chunk)
            if text is not None:
                yield text
        result = gov.flush()
        if result.needs_llm_fix:
            # 触发 LLM 自愈：把残缺 JSON 发回 LLM 修复
            pass
        yield result.output
    """

    def __init__(
        self,
        content_rules: Optional[List[ContentRule]] = None,
        max_buffer_size: int = 64 * 1024,  # 64KB
    ):
        self._content_rules = content_rules or DEFAULT_CONTENT_RULES
        self._max_buffer_size = max_buffer_size

        # 积累的完整文本（用于 JSON 修复）
        self._full_text: List[str] = []
        # 已输出的文本长度（用于增量输出）
        self._output_len: int = 0
        # 当前正在处理的块类型
        self._block_type: BlockType = BlockType.TEXT
        # 是否正在收集 tool_calls JSON
        self._collecting_json: bool = False
        # JSON 修复状态
        self._json_state = JSONRepairState()
        # JSON 起始偏移（在 _full_text 中的位置）
        self._json_start: int = -1

    # ====== 公共接口 ======

    def feed(self, chunk: str) -> Optional[str]:
        """处理一个流式 chunk。

        返回:
          - str: 应输出的文本（可能被拦截/替换）
          - None: 此 chunk 被缓冲（正在收集 tool_calls JSON）

        文本输出/ThinkingBlock：直接透传，检查非法内容。
        tool_calls JSON：缓冲累积，flush 时统一修复。
        """
        if not chunk:
            return None

        self._full_text.append(chunk)

        # 检测是否开始输出 tool_calls / thinking JSON
        if not self._collecting_json:
            start, block_type = self._detect_json_start()
            if start >= 0:
                if block_type == BlockType.THINKING:
                    # ThinkingBlock：透传不缓冲
                    self._block_type = BlockType.THINKING
                    new_text = "".join(self._full_text)[self._output_len:]
                    output = self._filter_content(new_text)
                    self._output_len = len("".join(self._full_text))
                    return output if output else None

                # ToolUseBlock：缓冲累积
                self._block_type = BlockType.TOOL_USE
                self._collecting_json = True
                self._json_start = start
                prefix = "".join(self._full_text)[self._output_len:start]
                self._output_len = start
                text_after = "".join(self._full_text)[start:]
                self._json_state.reset()
                for ch in text_after:
                    self._json_state.feed(ch)
                return prefix if prefix else None

        if self._collecting_json:
            # 累积 JSON，不输出
            text_after_json = "".join(self._full_text)[self._json_start:]
            self._json_state.reset()
            for ch in text_after_json:
                self._json_state.feed(ch)
            return None

        # 普通文本：检查非法内容后输出
        new_text = "".join(self._full_text)[self._output_len:]
        output = self._filter_content(new_text)
        self._output_len = len("".join(self._full_text))
        return output if output else None

    def flush(self) -> FlushResult:
        """流结束时调用，修复并输出最后的 tool_calls JSON。

        返回 FlushResult(output, repaired, confidence, needs_llm_fix)。
        Orchestrator 根据 needs_llm_fix 决定是否触发 LLM 自愈。
        """
        full = "".join(self._full_text)

        if self._collecting_json and self._json_start >= 0:
            json_text = full[self._json_start:]
            repaired, confidence = self._repair_json(json_text)
            self._output_len = len(full)
            self._collecting_json = False
            return FlushResult(
                output=repaired,
                repaired=(repaired != json_text),
                confidence=confidence,
                needs_llm_fix=(confidence == RepairConfidence.FAILED or confidence == RepairConfidence.LOW),
            )

        # 输出剩余文本
        remaining = full[self._output_len:]
        self._output_len = len(full)
        return FlushResult(
            output=remaining,
            repaired=False,
            confidence=RepairConfidence.HIGH,
            needs_llm_fix=False,
        )

    # ====== JSON 修复 ======

    def _repair_json(self, text: str) -> Tuple[str, RepairConfidence]:
        """修复残缺的 tool_calls JSON。返回 (repaired_text, confidence)。"""
        if not text:
            return text, RepairConfidence.HIGH

        text = text.strip()

        # 策略0：已是合法 JSON → 高置信度
        try:
            json.loads(text)
            return text, RepairConfidence.HIGH
        except json.JSONDecodeError:
            pass

        # 策略1：补全缺失的闭合括号 → 高置信度
        repaired = self._close_brackets(text)
        try:
            json.loads(repaired)
            return repaired, RepairConfidence.HIGH
        except json.JSONDecodeError:
            pass

        # 策略2：移除末尾不完整的键/值 → 中置信度
        repaired = self._trim_trailing_incomplete(text)
        repaired = self._close_brackets(repaired)
        try:
            json.loads(repaired)
            return repaired, RepairConfidence.MEDIUM
        except json.JSONDecodeError:
            pass

        # 策略3：修复字符串内缺闭合引号 → 低置信度
        repaired = self._fix_unclosed_string(text)
        repaired = self._close_brackets(repaired)
        try:
            json.loads(repaired)
            return repaired, RepairConfidence.LOW
        except json.JSONDecodeError:
            pass

        # 策略4：尾部逗号 → 中置信度
        repaired = self._remove_trailing_comma(text)
        repaired = self._close_brackets(repaired)
        try:
            json.loads(repaired)
            return repaired, RepairConfidence.MEDIUM
        except json.JSONDecodeError:
            pass

        # 兜底：不可修复 → 触发 LLM 自愈
        return json.dumps({"error": "unparseable tool_call arguments", "raw": text[:200]}), RepairConfidence.FAILED

    @staticmethod
    def _close_brackets(text: str) -> str:
        """补全缺失的闭合括号。"""
        stack: List[str] = []
        i = 0
        while i < len(text):
            ch = text[i]
            if ch == '"':
                # 跳过字符串
                i += 1
                while i < len(text):
                    if text[i] == '\\':
                        i += 2
                        continue
                    if text[i] == '"':
                        break
                    i += 1
            elif ch == '{':
                stack.append('}')
            elif ch == '[':
                stack.append(']')
            elif ch == '}':
                if stack and stack[-1] == '}':
                    stack.pop()
            elif ch == ']':
                if stack and stack[-1] == ']':
                    stack.pop()
            i += 1

        return text + "".join(reversed(stack))

    @staticmethod
    def _trim_trailing_incomplete(text: str) -> str:
        """移除末尾不完整的键值对。

        例: '{"path": "/tmp/te'  → 保留到最后一个完整的键值对为止
             '{"path": "/x", "con'  → '{"path": "/x"}'
        """
        # 找到最后一个完整的 : 分割的键值对
        # 简化策略：回到最后一个 "} 或 "], 或回到最后一个完整的 "key":
        last_complete = -1

        # 找最后一个逗号前的内容
        # 跳过字符串内部的逗号
        in_str = False
        escape = False
        for i in range(len(text) - 1, -1, -1):
            ch = text[i]
            if escape:
                escape = False
                continue
            if ch == '\\':
                escape = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if not in_str and ch == ',':
                last_complete = i
                break

        if last_complete > 0:
            prefix = text[:last_complete].rstrip()
            # 只有当前缀以 } 结尾时才截断（避免截断到字符串内的逗号）
            if prefix.endswith('"') or prefix.endswith('}') or prefix.endswith(']'):
                return prefix

        return text

    @staticmethod
    def _fix_unclosed_string(text: str) -> str:
        """修复字符串内缺少闭合引号。"""
        if not text:
            return text
        # 如果最后一个非空白字符是反斜杠，追加一个转义引号
        stripped = text.rstrip()
        if stripped.endswith('\\'):
            return text.rstrip()[:-1] + '"'
        # 如果最后一个字符不在闭合字符集中且不是引号，追加引号
        if stripped[-1] not in ('}', ']', '"'):
            # 检查是否在字符串中
            in_str = False
            escape = False
            for ch in stripped:
                if escape:
                    escape = False
                    continue
                if ch == '\\':
                    escape = True
                    continue
                if ch == '"':
                    in_str = not in_str
            if in_str:
                return text.rstrip() + '"'
        return text

    @staticmethod
    def _remove_trailing_comma(text: str) -> str:
        """移除尾部多余逗号。

        例: '{"a": 1,}' → '{"a": 1}'
        """
        stripped = text.rstrip()
        if stripped.endswith(','):
            return stripped[:-1].rstrip()
        return text

    # ====== JSON 检测 ======

    def _detect_json_start(self) -> Tuple[int, BlockType]:
        """检测 tool_calls / thinking JSON 的起始位置。

        返回 (offset, BlockType)，-1 表示未检测到。

        TODO(阶段3): 当前用正则匹配 "arguments"/"thinking" 关键词猜测块类型，
        这脆弱且依赖 OpenAI 格式。LLM 适配器实现后应改为：
          LLMInvoker 返回 StreamChunk(text, block_type)，OutputGovernor.feed()
          直接读取 block_type，删除本方法。
        """
        full = "".join(self._full_text)
        idx = self._output_len
        text = full[idx:]

        # 先检测 tool_use（优先级更高，因为 thinking 也可能出现在前面）
        match = re.search(r'"arguments"\s*:\s*"', text)
        if match:
            return idx + match.end(), BlockType.TOOL_USE

        # 检测 thinking 块
        match = re.search(r'"thinking"\s*:\s*"', text)
        if match:
            return idx + match.end(), BlockType.THINKING

        return -1, BlockType.TEXT

    def validate_tool_call(self, arguments_json: str) -> Tuple[bool, str]:
        """校验并修复工具调用参数 JSON。

        返回 (is_valid, repaired_json)。
        is_valid=False 表示修复失败，repaired_json 中包含错误信息。
        """
        # 尝试直接解析
        try:
            json.loads(arguments_json)
            return True, arguments_json
        except json.JSONDecodeError:
            pass

        # 尝试修复（_repair_json 返回 (str, confidence)）
        repaired, confidence = self._repair_json(arguments_json)
        try:
            data = json.loads(repaired)
            if isinstance(data, dict) and "error" in data and "raw" in data:
                return False, repaired
            return True, repaired
        except json.JSONDecodeError:
            return False, repaired

    # ====== 内容过滤 ======

    def _filter_content(self, text: str) -> str:
        """检查文本是否包含非法内容。

        返回过滤后的文本（可能被替换或截断）。
        """
        for rule in self._content_rules:
            if re.search(rule.pattern, text):
                if rule.severity == "block":
                    return f"[输出已拦截: {rule.description}]"
                elif rule.severity == "sanitize":
                    # 替换敏感内容
                    return re.sub(rule.pattern, "[REDACTED]", text)
                else:  # warn
                    # 透传，但附加警告标记
                    pass
        return text

    def add_rule(self, rule: ContentRule) -> None:
        """添加自定义内容规则"""
        self._content_rules.append(rule)

    def remove_rule(self, pattern: str) -> None:
        """移除内容规则"""
        self._content_rules = [
            r for r in self._content_rules if r.pattern != pattern
        ]

    @property
    def buffer_size(self) -> int:
        return len("".join(self._full_text))
