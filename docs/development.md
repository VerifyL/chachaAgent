## 环境搭建

### 前置条件

- Python ≥ 3.10（推荐 3.11+）
- Git 已安装并配置
- 终端编码 UTF-8

### 安装

```bash
git clone https://github.com/your-org/chachaAgent.git
cd chachaAgent

# 开发模式安装（可编辑 + 核心依赖）
pip install -e "."

# 安装全部开发工具（测试、格式化、热重载）
pip install -e ".[dev]"

# 含打包工具
pip install -e ".[dev,build]"
```

> 所有依赖由 `pyproject.toml` 统一管理，不再使用 `requirements.txt`（已保留供参考）。

---

## 模型配置

ChachaAgent 通过 `chachaConfig.toml` 配置模型提供商，支持 OpenAI / DeepSeek / Ollama 等兼容 API。

```toml
[model]
provider = "openai"           # openai | anthropic | ollama
model = "deepseek-chat"       # 模型名称
api_key = "sk-..."            # API Key（或设置环境变量 OPENAI_API_KEY）
base_url = ""                 # 兼容 API 地址（空=使用默认）
temperature = 0.7
max_tokens = 4096
```

**环境变量**：`OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `OLLAMA_HOST` 可替代配置文件中的 API Key。

---

## CLI 入口与子命令

ChachaAgent 通过 `chacha` 命令提供统一入口（`pyproject.toml` 中注册 `interface.cli.app:main`）。

### 基本用法

```bash
# 在目标项目目录中启动
cd /path/to/project && chacha

# 或指定项目路径
chacha /path/to/project
```

启动后进入交互式 CLI，输入消息直接发送。以下命令在 CL ✅ 可用 |

### CLI 内置命令

CLI 启动后在对话中使用 `/` 前缀命令：

| 命令 | 作用 |
|------|------|
| `/model <name>` | 切换模型 |
| `/url <url>` | 切换 API URL |
| `/key <sk->` | 设置 API Key |
| `/session` | 列出所有 session |
| `/session <#>` | 切换 session |
| `/session del <#>` | 删除 session |
| `/new` | 新建会话 |
| `/save` | 保存检查点 |
| `/memory` | 查看记忆文件 |
| `/dream` | 运行 Session Dream |
| `/dreamglobal` | 运行 GlobalDream |
| `/audit` | 审计报告 |
| `/status` | 系统状态 |
| `/compact` | 压缩上下文 |
| `/debug` | 调试模式开关 |
| `/help` | 帮助 |

快捷键: `Ctrl+N` 新会话 | `Ctrl+S` 保存 | `Ctrl+B` 会话列表 | `Ctrl+X` 压缩 | `Ctrl+J` 换行 | `Ctrl+L` 清屏 | `Ctrl+D` 退出

---

## 项目初始化

首次在项目目录运行 `chacha` 时自动初始化，无需手动操作。

### 自动初始化内容

CLI 启动时 (`ChachaCLI.initialize()`)：
- 无配置文件时自动生成 `~/.chacha/config.toml` 默认模板
- 无 CHACHA.md 时复制 `core/CHACHA.md.template` → `~/.chacha/CHACHA.md`
- 创建 MemoryManager（`.chacha_agent/` 目录结构按需自动创建）
- 会话检查点保存在 `.chacha_agent/checkpoints/`

1. **宪法文件**: 若 `~/.chacha/CHACHA.md` 不存在，从模板自动复制
2. **配置文件**: 若 `~/.chacha/config.toml` 不存在，自动生成默认模板
3. **记忆目录**: MemoryManager 自动创建 `.chacha_agent/projects/{hash}/memory/` 目录结构
4. **会话目录**: 每个 session 独立目录 `sessions/{session_id}/`，含 checkpoint + tool_cache

无需手动运行任何初始化脚本。

> `scripts/init_project.py` 已废弃，当前为薄包装。

---

## 调试与可观测性

ChachaAgent 通过 `core/telemetry.py` 提供统一可观测性。详细设计见 `docs/telemetry.md`。

### 日志文件

| 文件 | 位置 | 用途 |
|------|------|------|
| debug.jsonl | `.chacha_agent/logs/debug.jsonl` | 研发调试，5 级过滤（DEBUG/INFO/WARNING/ERROR/CRITICAL） |
| audit.jsonl | `.chacha_agent/logs/audit.jsonl` | 安全审计，记录工具调用、成本、记忆变更、权限审批 |

**查看日志**：

```bash
# 实时跟踪调试日志
tail -f .chacha_agent/logs/debug.jsonl | python -m json.tool

# 过滤特定级别
grep '"level":"ERROR"' .chacha_agent/logs/debug.jsonl

# 审计日志分析
cat .chacha_agent/logs/audit.jsonl | jq '.category'
```

**日志级别控制**：通过 `chachaConfig.toml` 的 `[telemetry]` 段设置：

```toml
[telemetry]
log_level = "DEBUG"     # DEBUG | INFO | WARNING | ERROR
enable_audit = true
```

环境变量覆盖：`CHA_CHA_TELEMETRY__LOG_LEVEL=WARNING`

### 指标（Metrics）

| 类型 | 说明 | 查询示例 |
|------|------|----------|
| counter | 累计计数 | LLM 调用次数、工具调用次数、压缩次数 |
| gauge | 瞬时值 | 上下文 token 数、累计成本、背压比率 |
| histogram | 分布 | LLM 延迟、工具耗时，支持 P50/P99 |

**Prometheus 端点**：

```toml
[telemetry]
enable_prometheus = true
prometheus_port = 9090
```

启动后访问 `http://localhost:9090/metrics` 获取 Prometheus 格式指标。

### 追踪（Tracing）

每个请求通过 `trace_id` 关联 LLM 调用→工具执行→响应全链路。Span 信息记录在 debug.jsonl 中。

### 运行测试

```bash
# 全量测试
pytest tests/ -v

# 仅可观测性测试
pytest tests/unit/test_telemetry.py -v

# 集成测试（全链路指标验证）
pytest tests/integration/test_telemetry_integration.py -v
```

---

## 会话持久化

`core/checkpoint_manager.py` 提供检查点保存与恢复，支持断点续传。

### 保存检查点

检查点全量保存 `ConversationState`（events + metadata + loop_state）到 `.chacha_agent/checkpoints/{session_id}/{checkpoint_id}.json`。

```python
from core.checkpoint_manager import CheckpointManager

mgr = CheckpointManager()
cp = mgr.save(state, description="高危操作前保存")
```

**自动保存时机**（Orchestrator 中集成）：
- 高危操作前（shell/write_file 等）
- 每 N 轮对话后
- 用户手动触发

### 恢复会话

```python
# 恢复最新检查点
state = mgr.restore("session-abc")

# 恢复指定检查点
state = mgr.restore("session-abc", checkpoint_id="ckpt-001")
```

### 管理检查点

```bash
# 列出检查点
python -c "from core.checkpoint_manager import CheckpointManager; \
  print(CheckpointManager().list('session-abc'))"

# 清理 72 小时前的旧检查点
python -c "from core.checkpoint_manager import CheckpointManager; \
  CheckpointManager().purge('session-abc', max_age_hours=72)"
```