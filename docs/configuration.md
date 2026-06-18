# ChachaAgent 配置文档

本文档详细说明 ChachaAgent 的配置文件 `chachaConfig.toml`（或兼容旧名称 `harness.toml`）的所有字段、环境变量覆盖方式以及各配置项的作用。

配置文件采用 [TOML](https://toml.io/) 格式，所有字段均有默认值，未显式配置的项将使用默认值。部分敏感字段（如 API Key）可通过环境变量覆盖，便于容器化部署。

---

## 顶层字段

| 字段名          | 类型                | 默认值    | 描述                                                                 |
|-----------------|---------------------|-----------|----------------------------------------------------------------------|
| `project_id`    | 字符串或 `null`     | `null`    | 项目唯一标识，用于隔离会话、记忆和检查点目录。若未设置，系统自动生成。 |
| `environment`   | `"dev"` 或 `"prod"` | `"dev"`   | 运行环境，影响日志详细程度和错误报告行为（生产环境将减少调试输出）。 |

**环境变量覆盖**：
- `CHA_CHA_PROJECT_ID`：覆盖 `project_id`
- `CHA_CHA_ENVIRONMENT`：覆盖 `environment`

---

## 模型管理层 `[model]`

配置 LLM 提供者、路由策略、重试和降级行为。

### 子字段

| 字段名                     | 类型                                      | 默认值       | 描述                                                                 |
|----------------------------|-------------------------------------------|--------------|----------------------------------------------------------------------|
| `providers`                | 表（Table），键为标识符，值为提供者配置    | **必填**     | 一个或多个模型提供者，键名可自定义（如 `default`、`claude`）。       |
| `router_strategy`          | `"priority"` / `"cost"` / `"random"`      | `"priority"` | 模型路由策略：按优先级（`priority`）、成本最低（`cost`）或随机选择。 |
| `fallback_chain`           | 字符串数组                                | `[]`         | 降级顺序，按提供者标识依次尝试，当前者失败时自动切换下一个。         |
| `retry_max_attempts`       | 整数 (≥1)                                 | `3`          | 调用失败时的最大重试次数（含首次）。                                  |
| `retry_backoff_factor`     | 浮点数 (≥0.1)                             | `1.0`        | 指数退避基数（秒），重试间隔为 `factor * 2^(n-1)`。                  |

### 提供者配置 `[model.providers.<name>]`

每个提供者必须包含以下字段：

| 字段名               | 类型                                       | 默认值    | 描述                                                                 |
|----------------------|--------------------------------------------|-----------|----------------------------------------------------------------------|
| `provider`           | `"openai"` / `"anthropic"` / `"ollama"`    | **必填**  | 模型提供商类型。                                                     |
| `api_key`            | 字符串或 `null`                            | `null`    | API 密钥，建议通过环境变量 `CHA_CHA_MODEL__PROVIDERS__<NAME>__API_KEY` 设置。 |
| `base_url`           | 字符串或 `null`                            | `null`    | 自定义 API 端点（如 Azure OpenAI 或代理地址），默认使用官方端点。   |
| `default_model`      | 字符串                                      | **必填**  | 该提供商下默认使用的模型名称（如 `"gpt-4"`、`"claude-3-opus"`）。   |
| `supports_vision`    | 布尔值                                      | `false`   | 该模型是否支持图像输入（v1.5+ 多模态功能使用）。                     |
| `cost_per_1k_input`  | 浮点数 (≥0)                                 | `0.0`     | 每 1000 个输入 token 的成本（美元），用于成本路由和熔断计算。        |
| `cost_per_1k_output` | 浮点数 (≥0)                                 | `0.0`     | 每 1000 个输出 token 的成本（美元）。                                 |

**环境变量覆盖**（以提供者标识 `default` 为例）：
- `CHA_CHA_MODEL__PROVIDERS__DEFAULT__API_KEY`
- `CHA_CHA_MODEL__PROVIDERS__DEFAULT__BASE_URL`
- `CHA_CHA_MODEL__PROVIDERS__DEFAULT__DEFAULT_MODEL`
- `CHA_CHA_MODEL__PROVIDERS__DEFAULT__SUPPORTS_VISION`
- `CHA_CHA_MODEL__PROVIDERS__DEFAULT__COST_PER_1K_INPUT`
- `CHA_CHA_MODEL__PROVIDERS__DEFAULT__COST_PER_1K_OUTPUT`
- `CHA_CHA_MODEL__ROUTER_STRATEGY`
- `CHA_CHA_MODEL__FALLBACK_CHAIN`（用逗号分隔，如 `"default,claude"`）
- `CHA_CHA_MODEL__RETRY_MAX_ATTEMPTS`
- `CHA_CHA_MODEL__RETRY_BACKOFF_FACTOR`

---

## 上下文管理 `[context]`

控制对话上下文窗口、压缩策略和记忆注入。

| 字段名                        | 类型                            | 默认值      | 描述                                                                 |
|-------------------------------|---------------------------------|-------------|----------------------------------------------------------------------|
| `max_tokens`                  | 整数 (≥1)                       | `128000`    | 上下文窗口的最大 token 数（含系统提示、历史消息、工具输出）。        |
| `compression_trigger_ratio`   | 浮点数 (0.5~1.0)                | `0.8`       | 触发压缩的 token 使用比例，如 0.8 表示当使用量达到 80% 时执行压缩。 |
| `memory_max_lines`            | 整数 (≥1)                       | `200`       | 核心记忆文件 `MEMORY.md` 的最大行数，超出则自动剪枝（保留最新）。    |
| `keep_system_prompt_first`    | 布尔值                          | `true`      | 系统提示是否始终位于消息列表最前端（避免被压缩排序影响）。           |
| `enable_summarization`        | 布尔值                          | `true`      | 是否启用 LLM 摘要压缩（若禁用，仅丢弃旧工具输出，不生成摘要）。      |
| `multimodal_compression`      | `"drop"` / `"describe"` / `"keep"` | `"keep"`  | **预留**：压缩时对多模态内容的处理策略。`drop` 丢弃，`describe` 转为文字描述，`keep` 保留原内容。 |

**环境变量覆盖**：
- `CHA_CHA_CONTEXT__MAX_TOKENS`
- `CHA_CHA_CONTEXT__COMPRESSION_TRIGGER_RATIO`
- `CHA_CHA_CONTEXT__MEMORY_MAX_LINES`
- `CHA_CHA_CONTEXT__KEEP_SYSTEM_PROMPT_FIRST`
- `CHA_CHA_CONTEXT__ENABLE_SUMMARIZATION`
- `CHA_CHA_CONTEXT__MULTIMODAL_COMPRESSION`

---

## 记忆存储 `[memory]`

配置 `.chacha_agent/memory/` 目录下的记忆持久化行为。

| 字段名                        | 类型          | 默认值                                      | 描述                                                              |
|-------------------------------|---------------|---------------------------------------------|-------------------------------------------------------------------|
| `project_dir`                 | 路径字符串    | `".chacha_agent/memory"` (相对于项目根)     | 记忆存储根目录。                                                  |
| `auto_clean_interval_hours`   | 整数 (≥1)     | `24`                                        | 后台自动清理（Auto Dream）的执行间隔（小时）。                   |
| `max_topic_files`             | 整数 (≥1)     | `10`                                        | 主题文件（`topics/*.md`）的最大数量，超出按 LRU 清理。           |

**环境变量覆盖**：
- `CHA_CHA_MEMORY__PROJECT_DIR`
- `CHA_CHA_MEMORY__AUTO_CLEAN_INTERVAL_HOURS`
- `CHA_CHA_MEMORY__MAX_TOPIC_FILES`

---

## 沙箱执行器 `[sandbox]`

控制命令执行的安全边界和资源限制。

| 字段名                | 类型            | 默认值（列表）                                                                   | 描述                                                                 |
|-----------------------|-----------------|----------------------------------------------------------------------------------|----------------------------------------------------------------------|
| `allowed_commands`    | 字符串数组      | `["ls","cat","grep","python","pytest","git","echo","head","tail"]`              | 允许执行的命令前缀白名单（子串匹配，建议精确）。                    |
| `timeout_seconds`     | 整数 (1~3600)   | `60`                                                                             | 单个命令的超时时间（秒）。                                           |
| `max_output_lines`    | 整数 (≥1)       | `1000`                                                                           | 命令输出最大行数，超出则截断（防止内存溢出）。                      |
| `working_dir`         | 路径或 `null`   | `null`                                                                           | 命令执行的工作目录，默认使用项目根目录。                             |

**环境变量覆盖**：
- `CHA_CHA_SANDBOX__ALLOWED_COMMANDS`（逗号分隔，如 `"ls,cat,grep"`）
- `CHA_CHA_SANDBOX__TIMEOUT_SECONDS`
- `CHA_CHA_SANDBOX__MAX_OUTPUT_LINES`
- `CHA_CHA_SANDBOX__WORKING_DIR`

---

## 安全策略 `[policy]`

成本控制、命令黑名单和审批缓存策略。

| 字段名                        | 类型            | 默认值（列表）                                                              | 描述                                                                 |
|-------------------------------|-----------------|-----------------------------------------------------------------------------|----------------------------------------------------------------------|
| `command_blacklist`           | 字符串数组      | `["rm -rf","sudo","chmod 777","dd","mkfs"]`                                | 禁止匹配的命令关键字（子串匹配，命中即拒绝）。                      |
| `cost_limit_dollars`          | 浮点数 (≥0)     | `10.0`                                                                      | 单次会话的累计成本上限（美元），`0` 表示不限制。                    |
| `approval_cache_ttl_seconds`  | 整数 (≥0)       | `300`                                                                       | 工具审批结果的缓存时间（秒），`0` 表示不缓存（每次均询问用户）。   |

**环境变量覆盖**：
- `CHA_CHA_POLICY__COMMAND_BLACKLIST`（逗号分隔）
- `CHA_CHA_POLICY__COST_LIMIT_DOLLARS`
- `CHA_CHA_POLICY__APPROVAL_CACHE_TTL_SECONDS`

---

## 可观测性 `[telemetry]`

日志、审计和 Prometheus 指标配置。

| 字段名                | 类型                                    | 默认值                                     | 描述                                                              |
|-----------------------|-----------------------------------------|--------------------------------------------|-------------------------------------------------------------------|
| `log_level`           | `"DEBUG"`/`"INFO"`/`"WARNING"`/`"ERROR"`| `"INFO"`                                   | 日志级别。                                                       |
| `enable_audit`        | 布尔值                                  | `true`                                     | 是否写入审计日志（`audit.jsonl`）。                               |
| `enable_prometheus`   | 布尔值                                  | `false`                                    | 是否暴露 Prometheus `/metrics` 端点。                            |
| `prometheus_port`     | 整数 (1~65535)                         | `9090`                                     | Prometheus 监听端口。                                             |
| `audit_log_path`      | 路径字符串                              | `".chacha_agent/logs/audit.jsonl"`         | 审计日志文件路径。                                               |
| `debug_log_path`      | 路径字符串                              | `".chacha_agent/logs/debug.jsonl"`         | 调试日志文件路径。                                               |

**环境变量覆盖**：
- `CHA_CHA_TELEMETRY__LOG_LEVEL`
- `CHA_CHA_TELEMETRY__ENABLE_AUDIT`
- `CHA_CHA_TELEMETRY__ENABLE_PROMETHEUS`
- `CHA_CHA_TELEMETRY__PROMETHEUS_PORT`
- `CHA_CHA_TELEMETRY__AUDIT_LOG_PATH`
- `CHA_CHA_TELEMETRY__DEBUG_LOG_PATH`

---

## 多模态预留 `[multimodal]` （v1.5+）

> **注意**：此配置段在当前版本（v1.0）仅作为占位，多模态功能将在 v1.5 版本中正式启用。您可以在配置中预先设置相关参数，系统会加载它们但不会生效，以确保未来升级时无需修改配置。

| 字段名                   | 类型                | 默认值    | 描述                                                                 |
|--------------------------|---------------------|-----------|----------------------------------------------------------------------|
| `enabled`                | 布尔值              | `false`   | 是否启用多模态功能（v1.5 起可用）。                                  |
| `vision_model`           | 字符串或 `null`     | `null`    | 指定用于视觉任务的模型名称，若为空则自动选择 `supports_vision=true` 的首个提供商。 |
| `max_image_size_mb`      | 整数 (≥1)          | `10`      | 单张图片的最大大小（MB），超限将拒绝或压缩。                         |
| `enable_ocr_fallback`    | 布尔值              | `true`    | 当图片解析失败时是否降级为 OCR 文本提取。                            |

**环境变量覆盖**：
- `CHA_CHA_MULTIMODAL__ENABLED`
- `CHA_CHA_MULTIMODAL__VISION_MODEL`
- `CHA_CHA_MULTIMODAL__MAX_IMAGE_SIZE_MB`
- `CHA_CHA_MULTIMODAL__ENABLE_OCR_FALLBACK`

---

## 表现层 `[interface]`

控制 CLI 和 Web 界面的行为。

| 字段名                     | 类型                                     | 默认值       | 描述                                                                 |
|----------------------------|------------------------------------------|--------------|----------------------------------------------------------------------|
| `cli_theme`                | `"dark"` / `"light"` / `"default"`       | `"default"`  | CLI 终端配色主题。                                                   |
| `cli_enable_ansi_parser`   | 布尔值                                   | `true`       | 是否在 CLI 中解析并渲染 ANSI 转义序列（颜色、样式）。               |
| `web_enabled`              | 布尔值                                   | `false`      | 是否启动 Web 服务器。                                                |
| `web_host`                 | 字符串                                   | `"127.0.0.1"`| Web 服务监听地址。                                                   |
| `web_port`                 | 整数 (1~65535)                          | `8080`       | Web 服务端口。                                                       |
| `web_auth_required`        | 布尔值                                   | `false`      | 是否启用多用户认证（需要额外配置用户数据库）。                       |

**环境变量覆盖**：
- `CHA_CHA_INTERFACE__CLI_THEME`
- `CHA_CHA_INTERFACE__CLI_ENABLE_ANSI_PARSER`
- `CHA_CHA_INTERFACE__WEB_ENABLED`
- `CHA_CHA_INTERFACE__WEB_HOST`
- `CHA_CHA_INTERFACE__WEB_PORT`
- `CHA_CHA_INTERFACE__WEB_AUTH_REQUIRED`

---

## 环境变量覆盖规则

所有配置项均可通过环境变量覆盖，规则如下：

- 环境变量名格式：`CHA_CHA_` + 配置路径（顶层字段名或嵌套路径）。
- 嵌套路径使用双下划线 `__` 分隔，且**全部转为小写**。
- 例如：`model.providers.default.api_key` → `CHA_CHA_MODEL__PROVIDERS__DEFAULT__API_KEY`。
- 数组类型（如 `allowed_commands`、`fallback_chain`、`command_blacklist`）可用逗号分隔的字符串表示。
- 布尔值支持 `true`/`false`（不区分大小写），数字（整数或浮点数）自动转换。
- 只有顶层字段名在配置模型中存在的环境变量才会被应用，其他以 `CHA_CHA_` 开头的变量将被忽略（不会导致错误）。

这种设计允许您在容器化环境（如 Kubernetes）中灵活调整配置，而无需修改配置文件本身。

---

## 配置文件示例

以下是一个完整的 `chachaConfig.toml` 示例，涵盖所有配置段：

```toml
project_id = "my-awesome-project"
environment = "prod"

[model]
router_strategy = "cost"
fallback_chain = ["default", "claude"]
retry_max_attempts = 5
retry_backoff_factor = 2.0

[model.providers.default]
provider = "openai"
api_key = "sk-..."           # 建议通过环境变量设置
default_model = "gpt-4"
supports_vision = false
cost_per_1k_input = 0.01
cost_per_1k_output = 0.03

[model.providers.claude]
provider = "anthropic"
api_key = "sk-ant-..."       # 建议通过环境变量设置
default_model = "claude-3-opus"
supports_vision = true
cost_per_1k_input = 0.015
cost_per_1k_output = 0.075

[context]
max_tokens = 200000
compression_trigger_ratio = 0.85
memory_max_lines = 150
keep_system_prompt_first = false
enable_summarization = true
multimodal_compression = "describe"

[memory]
project_dir = ".chacha_agent/memory"
auto_clean_interval_hours = 12
max_topic_files = 20

[sandbox]
allowed_commands = ["ls", "cat", "grep", "python", "pytest", "git"]
timeout_seconds = 30
max_output_lines = 500

[policy]
command_blacklist = ["rm", "sudo", "chmod"]
cost_limit_dollars = 5.0
approval_cache_ttl_seconds = 600

[telemetry]
log_level = "DEBUG"
enable_audit = true
enable_prometheus = true
prometheus_port = 9091

[multimodal]           # v1.5 预留
enabled = true
vision_model = "gpt-4-vision"
max_image_size_mb = 20
enable_ocr_fallback = false

[interface]
cli_theme = "dark"
web_enabled = true
web_host = "0.0.0.0"
web_port = 8080
```

---

## 静态规则 (CHACHA.md)

ChachaAgent 支持通过 `CHACHA.md` 文件按目录层级声明静态行为规则，参考 Claude Code 的 `.claude/CLAUDE.md` 机制。详见 `docs/static_rule_loader.md`。

### 加载顺序

```
~/.chacha/CHACHA.md              # 用户级全局规则（最先加载）
{project_root}/CHACHA.md         # 项目级规则
{project_root}/{sub_dir}/CHACHA.md  # 子目录级规则（最后加载，追加到末尾）
```

### @import 指令

CHACHA.md 内可使用 `@import` 引用其他文件：

```markdown
# 项目 CHACHA.md
使用 Python 3.11+
@import ./rules/coding-style.md
@import ~/.chacha/shared/python.md
```

### 注入 ContextManager

`StaticRuleLoader` 加载的规则文本直接传递给 `ContextManager.assemble(static_rules=...)`，作为 `protected` 区的上下文块，优先于对话历史。配置文件本身不包含规则内容，规则内容由独立文件承载。

```
```