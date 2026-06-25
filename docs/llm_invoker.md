# 模型管理指南 (`core/llm_invoker.py`)

本文档说明 LLM 调用器的接口设计、流式处理、工具调用解析和与模型适配器的集成。`LLMInvoker` 位于 Orchestrator 与模型适配器之间，负责流式输出、增量解析和遥测。

> **注意**：本文档当前覆盖 LLM 调用器（阶段 2.6）。模型适配器（OpenAI/Anthropic/Ollama 客户端）在阶段 3 实现，届时将补充适配器开发指南。

## 概述

LLMInvoker 与模型适配器通过最小接口解耦。适配器只需实现一个方法：

```python
async def stream(messages: List[Dict], tools: List[Dict]) -> AsyncIterator[StreamChunk]:
    ...
```

### 架构位置

```
Orchestrator
  │
  ├─ ContextManager → messages[]
  ├─ LLMInvoker.invoke(messages, tools, session_id)   ← 这里
  │    ├─ PolicyEngine.evaluate_cost()   → 熔断检查
  │    ├─ model_client.stream()          → 流式调用
  │    ├─ Gateway.publish(TokenChunkEvent) → 前端推送
  │    ├─ OutputGovernor.validate_tool_call() → JSON 修复
  │    └─ Telemetry.agent.record_llm_call() → 指标记录
  │
  └─ → LLMResponse(text, tool_calls, usage, finish_reason)
```

---

## 1. 适配器接口

### 1.1 StreamChunk（Pydantic Discriminated Union）

`StreamChunk` 是 7 个子类的 discriminated union（`core/llm_invoker.py`），适配器产出具体子类实例：

```python
class TextChunk(BaseModel):
    type: Literal["text"] = "text"
    content: str

class ReasoningChunk(BaseModel):
    type: Literal["reasoning"] = "reasoning"
    content: str

class ToolCallStartChunk(BaseModel):
    type: Literal["tool_call_start"] = "tool_call_start"
    tool_index: int
    tool_id: str
    tool_name: str = ""

class ToolCallDeltaChunk(BaseModel):
    type: Literal["tool_call_delta"] = "tool_call_delta"
    tool_index: int
    tool_args_delta: str = ""

class ToolCallEndChunk(BaseModel):
    type: Literal["tool_call_end"] = "tool_call_end"
    tool_index: int
    tool_args_delta: str = ""

class DoneChunk(BaseModel):
    type: Literal["done"] = "done"
    finish_reason: str = ""       # stop | tool_calls | length | content_filter
    usage: Optional[Dict[str, Any]] = None

class ErrorChunk(BaseModel):
    type: Literal["error"] = "error"
    error: Optional[str] = None
    content: str = ""
```

适配器（OpenAI/Anthropic 等）将各自的流式响应转换为具体的 Chunk 子类实例。消费方用 `isinstance()` 匹配：

```python
async for chunk in client.stream(...):
    if isinstance(chunk, TextChunk):
        print(chunk.content, end="")
    elif isinstance(chunk, ToolCallStartChunk):
        tool_calls[chunk.tool_index] = ToolCall(id=chunk.tool_id, name=chunk.tool_name, arguments={})
```

**流式示例**：

```
TextChunk("Let me") → ToolCallStartChunk(tool_index=0, tool_id="c1", tool_name="read_file")
→ ToolCallDeltaChunk(tool_index=0, tool_args_delta='{"pa')
→ ToolCallDeltaChunk(tool_index=0, tool_args_delta='th":"/tmp/test.py"}')
→ ToolCallEndChunk(tool_index=0) → DoneChunk(finish_reason="tool_calls")
```

### 1.2 ToolCall

```python
class ToolCall(BaseModel):
    id: str                              # 工具调用 ID
    name: str                            # 工具名称
    arguments: Dict[str, Any] = Field(default_factory=dict)
```

### 1.3 LLMResponse

```python
class LLMResponse(BaseModel):
    text: str = ""                         # 文本输出（所有 text chunk 拼接）
    tool_calls: List[ToolCall] = Field(default_factory=list)
    finish_reason: str = "stop"            # stop | tool_calls | length | content_filter
    error: Optional[str] = None
    usage: Dict[str, int] = Field(default_factory=dict)  # {input, output, total}
    duration_ms: int = 0
```

---

## 2. 使用示例

### 2.1 基本调用

```python
invoker = LLMInvoker(model_client=openai_adapter, telemetry=telemetry)

resp = await invoker.invoke(
    messages=[{"role": "user", "content": "Read main.py"}],
    tools=[{"type": "function", "function": {"name": "read_file", ...}}],
    session_id="session-abc",
)

if resp.error:
    print(f"Error: {resp.error}")
elif resp.tool_calls:
    for tc in resp.tool_calls:
        print(f"→ {tc.name}({tc.arguments})")
else:
    print(resp.text)
```

### 2.2 流式推送到前端

```python
# Gateway 已注入时，text chunk 自动实时推送
invoker = LLMInvoker(
    model_client=adapter,
    gateway=gateway,  # ← 注入后在 invoke 内部自动 publish
)

resp = await invoker.invoke(messages, tools, "session-abc")
# gateway.publish() 在内部被 token 流驱动，前端实时渲染
```

### 2.3 无模型客户端时（测试/开发）

```python
invoker = LLMInvoker()  # model_client=None
resp = await invoker.invoke(messages)
# → LLMResponse(error="No model client configured")
```

---

## 3. 异常映射

| 异常类型 | 映射信息 |
|---------|---------|
| `429` / `rate` | `Rate limited: ...` |
| `401` / `403` | `Authentication error: ...` |
| `timeout` | `Timeout: ...` |
| 其他 | `{ExceptionName}: {message}` |

---

## 4. 与现有模块的联动

| 模块 | 调用时机 |
|------|----------|
| `PolicyEngine` | invoke 开始时 `evaluate_cost(0.0)` 检查熔断 |
| `Gateway` | 每个 text chunk 调用 `publish(TokenChunkEvent)` |
| `OutputGovernor` | 流结束后 `validate_tool_call(args_raw)` 修复残缺 JSON |
| `Telemetry` | invoke 结束时 `record_llm_call(model, input, output, latency, success)` |

---

## 5. 阶段 3 适配器开发指南（预留）

> TODO(阶段3): 实现 `core/llm_clients/openai_client.py`、`anthropic_client.py`、`ollama_client.py`。
> 各适配器只需将 API 响应转换为 `StreamChunk` 序列即可接入 LLMInvoker。

**OpenAI 流式转换示例（预留）**：

```python
# core/llm_clients/openai_client.py（阶段 3）
from core.llm_invoker import TextChunk, ToolCallStartChunk, ToolCallDeltaChunk, DoneChunk, StreamChunk

class OpenAIClient:
    async def stream(self, messages, tools) -> AsyncIterator[StreamChunk]:
        response = await self._client.chat.completions.create(
            model=self.model, messages=messages, tools=tools, stream=True,
        )
        async for event in response:
            delta = event.choices[0].delta
            if delta.content:
                yield TextChunk(content=delta.content)
            if delta.tool_calls:
                for tc in delta.tool_calls:
                    if tc.id:
                        yield ToolCallStartChunk(tool_index=tc.index,
                                                 tool_id=tc.id, tool_name=tc.function.name)
                    if tc.function.arguments:
                        yield ToolCallDeltaChunk(tool_index=tc.index,
                                                 tool_args_delta=tc.function.arguments)
        yield DoneChunk(finish_reason=..., usage=...)
```
