# 记忆管理系统

三层设计（参考 Claude Code）：

```
write:  LLM 调用 remember 工具 → 按日期写入 *.md 文件
read:   LLM 调用 load_memory 工具 → 搜索所有文件
dream:  会话结束后异步运行 → 整合为 MEMORY.md 索引
```

| 模块 | 文件 |
|------|------|
| 记忆读写 | `core/context/memory_manager.py` |
| 整合管道 | `core/context/dream.py` |
| 上下文注入 | `core/context_manager.py` |

---

## 1. 每日文件（raw 层）

LLM 通过 `remember` 工具写入。按日期分散存储：

```
.chacha_agent/memory/projects/p1/memory/
  2026-06-15.md    ← "偏好 Python 3.11", "项目使用 ruff"
  2026-06-18.md    ← "部署使用 Docker"
```

`search(query)` 跨所有日期文件搜索相关条目。

## 2. MEMORY.md 索引（autoDream）

`DreamPipeline` 会话结束后异步运行，1 次 LLM 调用整合所有每日文件：

```
Gather: 读取所有 *.md → Consolidate: LLM 总结为 200 条精华 → Write: MEMORY.md
```

`ContextManager.assemble(memory_manager=mgr)` 自动加载 MEMORY.md 注入上下文。

**触发条件**：距上次整合 > 24 小时。

## 3. 使用

```python
mgr = MemoryManager(project_id="p1")
mgr.remember("偏好 Python 3.11")      # LLM 工具
mgr.search("Python")                  # LLM 工具
mgr.read()                            # ContextManager 自动

dream = DreamPipeline(llm_invoker)
await dream.run(mgr)                  # 会话结束后

# 自动摘要
from core.context.summarizer import Summarizer
summarizer = Summarizer(llm_invoker)
summary = await summarizer.summarize(old_messages, style="brief")
summary = await summarizer.summarize(raw_memory, style="detailed")  # autoDream 用
```

`Summarizer` 被 `ContextCompressor`（SUMMARIZED 阶段）和 `DreamPipeline`（CONSOLIDATED 阶段）共用，避免 prompt 模板重复。
```

---

## 4. 压缩机制

> 详见 `docs/context_compressor.md`、`core/context/context_compressor.py`

上下文超限时，渐进式压缩（参考 Claude Code）：

| Level | 触发条件 | 操作 |
|-------|---------|------|
| FROZEN | `needs_compression=True` | 工具结果 → 占位符 + 缓存文件（LLM 可通过 read_file 查看完整） |
| TRIMMED | FROZEN 后仍超限 | 历史消息 → 首尾裁剪（动态比例 = 1 - pressure） |
| SUMMARIZED | TRIMMED 后仍超限 | 最旧消息 → LLM 摘要 |

**永不动**：system_prompt / CHACHA.md / skills / 最近 5 轮。

## 5. 配置

```toml
[memory]
prune_days = 30           # 每日文件保留天数
max_memory_lines = 200    # MEMORY.md 最大条目数

[context]
enable_memory_injection = true  # 是否注入 MEMORY.md 索引
```
