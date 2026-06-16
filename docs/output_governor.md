# 输出治理器 (`core/output_governor.py`)

本文档说明 `OutputGovernor` 的流式块识别、JSON 修复、内容过滤和 LLM 自愈机制。输出治理器位于 LLMInvoker 与外部之间，是 **Harness 四步防线的第 1、2、4 步**（格式校正 + LLM 自愈 + 安全检查）。

## 概述

> Harness 工程指南："LLM 的输出是不可预测的。即使你在提示词中要求'请返回 JSON 格式'，LLM 也可能返回格式不完整的 JSON。输出治理就是应对这种不确定性的防线。"

设计融合了 **Harness 四步防线 + Claude Code 结构化块类型**：

- **块类型识别**：区分 TextBlock（纯文本）→ 透传 / ToolUseBlock（工具调用）→ 缓冲修复 / ThinkingBlock（思考过程）→ 透传
- **5 级 JSON 修复**：补括号 → 截断不完整键 → 补引号 → 去尾逗号 → 兜底错误包装
- **修复置信度**：每级修复附带置信度（HIGH/MEDIUM/LOW/FAILED），指导 LLM 自愈决策
- **LLM 自愈**：机械修复失败时返回 `needs_llm_fix=True`，Orchestrator 将残缺 JSON 发回 LLM："请修复这段 JSON"
- **内容过滤**：正则规则 + 关键字匹配，支持 `block` / `sanitize` / `warn` 三种策略

### 流式处理流程

```
LLMInvoker 输出 chunk
  │
  ├─ _detect_json_start() → 检测块类型
  │
  ├─ ThinkingBlock ("thinking": "..." 开头)
  │     → 透传，不缓冲，不修复
  │
  ├─ ToolUseBlock ("arguments": "..." 开头)
  │     → 缓冲累积 all chunks
  │     → flush() 时统一修复 JSON
  │     → 返回 FlushResult(output, repaired, confidence, needs_llm_fix)
  │
  └─ TextBlock（其他）
        → _filter_content() 检查非法内容
        → 透传（可能被 block/sanitize）
```

---

## 1. 块类型枚举

```python
class BlockType(str, Enum):
    TEXT = "text"          # 纯文本 → 透传 + 内容过滤
    TOOL_USE = "tool_use"  # 工具调用 JSON → 缓冲累积后修复
    THINKING = "thinking"  # 思考过程 → 透传（不缓冲）
```

**检测规则**：`_detect_json_start()` 扫描累积文本，匹配 `"arguments": "` → `TOOL_USE`，匹配 `"thinking": "` → `THINKING`，否则 `TEXT`。

**为什么 ThinkingBlock 不缓冲？** 思考内容是需要实时展示的，不是工具调用参数。Claude 的 `thinking` 块也是作为独立 ContentBlock 流过。缓冲思考内容会导致用户看到卡顿。

---

## 2. 修复置信度

```python
class RepairConfidence(str, Enum):
    HIGH = "high"        # 仅补括号 → 几乎确定正确
    MEDIUM = "medium"    # 截断/去尾逗号 → 可能丢失了部分参数
    LOW = "low"          # 补引号 → 修复不精确
    FAILED = "failed"    # 完全不可修复 → 触发 LLM 自愈
```

| 置信度 | 修复策略 | LLM 自愈 | 原因 |
|--------|---------|---------|------|
| `HIGH` | 补括号 | ❌ 不需要 | 只缺闭合符号，内容完整 |
| `MEDIUM` | 截断不完整键值 | ❌ 不需要 | 可能有丢失，但结构正确 |
| `LOW` | 补引号 | ✅ 触发 | 引号边界不确定，可能修复错 |
| `FAILED` | 兜底错误包装 | ✅ 触发 | 机器无法修复 |

---

## 3. JSON 修复策略

### 3.1 `_repair_json(text)` → `(str, RepairConfidence)`

**策略递进**：

```
strategy 0: 已是合法 JSON                                         → HIGH
strategy 1: _close_brackets() 补全缺失的 {}、[]                    → HIGH
strategy 2: _trim_trailing_incomplete() 截断不完整键值对            → MEDIUM
strategy 3: _fix_unclosed_string() 修复未闭合引号                   → LOW
strategy 4: _remove_trailing_comma() 移除尾部逗号 + 补括号          → MEDIUM
strategy fallback: 兜底 → json.dumps({"error": ..., "raw": ...})   → FAILED
```

**示例**：

| 输入 | 修复后 | 置信度 |
|------|--------|--------|
| `{"path": "/tmp", "args": {"key": "val"}` | `{"path": "/tmp", "args": {"key": "val"}}` | HIGH |
| `{"path": "/x", "con` | `{"path": "/x"}` | MEDIUM |
| `{"key": "val` | `{"key": "val"}` | LOW |
| `not json at all {{{` | `{"error": "...", "raw": "..."}` | FAILED |

### 3.2 `validate_tool_call(json)` → `(bool, str)`

校验工具调用参数 JSON，返回 `(is_valid, repaired_json)`。如果修复后是兜底错误包装，返回 `(False, ...)`。

```python
gov = OutputGovernor()
valid, repaired = gov.validate_tool_call('{"path": "/tmp/main.py"')
if valid:
    args = json.loads(repaired)
```

---

## 4. flush() 与 LLM 自愈

### 4.1 FlushResult

```python
@dataclass
class FlushResult:
    output: str               # 修复后的文本
    repaired: bool            # 是否进行了修复
    confidence: RepairConfidence
    needs_llm_fix: bool       # True → Orchestrator 触发 LLM 自愈
```

### 4.2 自愈触发的典型场景

```
1. LLM 输出了 tool_calls JSON，但格式严重残缺
2. OutputGovernor.flush() → needs_llm_fix=True
3. Orchestrator 收到标志 → 构建修复提示："请修复以下 JSON: {...}"
4. 将修复提示作为新的 LLM 请求发送
5. LLM 返回修复后的 JSON
6. 重新解析 → 正常执行工具调用
```

---

## 5. 内容过滤

### 5.1 ContentRule

```python
@dataclass
class ContentRule:
    pattern: str        # 正则表达式
    description: str    # 规则说明
    severity: str = "block"  # block | sanitize | warn
```

### 5.2 默认规则

| 规则 | 模式 | 策略 |
|------|------|------|
| 危险命令 | `rm -rf /` | `block`（拦截） |
| 特权命令 | `sudo` | `block`（拦截） |
| API Key 泄露 | `api_key=...` | `sanitize`（脱敏为 `[REDACTED]`） |

### 5.3 自定义规则

```python
gov = OutputGovernor()
gov.add_rule(ContentRule(r"(?i)company_secret_123", "公司机密", severity="block"))
gov.remove_rule(original_pattern)
```

---

## 6. 使用示例

### 6.1 基本流式处理

```python
gov = OutputGovernor()

async for chunk in llm_stream:
    output = gov.feed(chunk)
    if output:
        await send_to_frontend(output)

result = gov.flush()
if result.needs_llm_fix:
    # Orchestrator 触发 LLM 自愈
    fixed = await ask_llm_to_fix(result.output)

await send_to_frontend(result.output)
```

### 6.2 纯文本场景（无 tool_calls）

```python
gov = OutputGovernor()
gov.feed("Hello, ")
gov.feed("world!")

result = gov.flush()
# result.output = ""
# result.needs_llm_fix = False
```

### 6.3 安全过滤场景

```python
gov = OutputGovernor()
# 添加项目级安全规则
gov.add_rule(ContentRule(r"(?i)production_db_password", "生产数据库密码"))

output = gov.feed("The password is production_db_password=xyz123")
assert "已拦截" in output
```

---

## 7. 与 Harness 四步防线的对应

| Harness 四步 | OutputGovernor 对应 |
|-------------|-------------------|
| 1. JSON 解析 | `_repair_json()` 5级机械修复 |
| 2. LLM 自愈 | `FlushResult.needs_llm_fix` → Orchestrator 发回 LLM |
| 3. 语义验证 | `PolicyEngine`（阶段 2.3，不在本模块） |
| 4. 安全检查 | `_filter_content()` + `ContentRule` 规则集 |
