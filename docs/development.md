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
chacha <子命令> [选项]
```

### 子命令一览

| 子命令 | 作用 | 状态 |
|--------|------|------|
| `chacha run --mode cli` | 启动 Textual TUI 终端界面 | 📋 阶段 7 |
| `chacha run --mode web [--port 8080]` | 启动 FastAPI Web 服务 | 📋 阶段 8 |
| `chacha init [-p ID] [-f]` | 初始化项目目录和配置 | ✅ 可用 |
| `chacha config [--validate-only]` | 校验并打印当前配置 | ✅ 可用 |

### `run` 命令

```bash
# 启动 CLI 终端界面（默认）
python main.py run
python main.py run --mode cli

# 启动 Web 服务
python main.py run --mode web
python main.py run --mode web --port 3000 --host 0.0.0.0
```

| 选项 | 默认值 | 说明 |
|------|--------|------|
| `--mode` | `cli` | 运行模式：`cli` 或 `web` |
| `--port` | `8080` | Web 模式监听端口 |
| `--host` | `127.0.0.1` | Web 模式监听地址 |

### `init` 命令

```bash
# 默认初始化
python main.py init

# 指定项目 ID
python main.py init -p my_project

# 强制覆盖
python main.py init --force
```

### `config` 命令

```bash
# 打印当前配置
python main.py config

# 仅校验，不打印
python main.py config --validate-only
```

---

## 项目初始化

首次使用 ChachaAgent 前，需要创建运行时目录和必要的配置文件。项目提供了自动化初始化脚本 `scripts/init_project.py`。

### 使用方法

```bash
python scripts/init_project.py [选项]
```

### 参数说明

| 参数 | 简写 | 说明 | 默认值 |
|------|------|------|--------|
| `--project-id` | `-p` | 项目标识符，用于隔离不同项目的记忆和配置 | 环境变量 `PROJECT_ID` 或 `"default"` |
| `--force`    | `-f` | 强制覆盖已存在的目录或文件（谨慎使用） | `False` |

### 示例

```bash
# 使用默认项目 ID
python scripts/init_project.py

# 指定项目 ID
python scripts/init_project.py -p my_project

# 强制重置所有运行时数据
python scripts/init_project.py --force
```

### 生成的内容

脚本会在项目根目录下创建 `.chacha_agent/` 运行时目录，包含：

- `checkpoints/`：会话检查点
- `memory/projects/<project_id>/memory/`：核心记忆文件 `MEMORY.md`
- `memory/projects/<project_id>/topics/`：主题文件
- `rag_store/`：向量库存储
- `logs/`：双轨日志（`debug.jsonl`、`audit.jsonl`）

同时，脚本会在当前目录生成默认配置文件 `chachaConfig.toml`（如果不存在），您可以根据需要修改。

> **注意**：`--force` 会**删除**并重建整个 `.chacha_agent` 目录，请确保没有未保存的重要会话数据。

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