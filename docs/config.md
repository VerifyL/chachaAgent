# ChachaAgent 配置数据结构字段说明

本文档详细说明 `core/models/config.py` 中定义的所有配置模型及其字段，对应项目级 `chachaConfig.toml`（或全局 `~/.chacha/config.toml`，结构一致）配置文件。

---

## 顶层配置 `ChaChaConfig`

| 字段名          | 类型                | 默认值    | 描述                                                                 |
|-----------------|---------------------|-----------|----------------------------------------------------------------------|
| `project_id`    | `str` 或 `None`     | `None`    | 项目唯一标识，用于隔离会话和记忆目录。若为空则自动生成。              |
| `environment`   | `"dev"` 或 `"prod"` | `"dev"`   | 运行环境，影响日志详细程度和错误报告行为。                            |
| `model`         | `ModelConfig`       | **必填**  | 模型管理层配置，见下表。                                              |
| `context`       | `ContextConfig`     | 见下文    | 上下文与记忆管理配置。                                                |
| `memory`        | `MemoryConfig`      | 见下文    | 记忆存储（`.chacha/memory`）配置。                              |
| `sandbox`       | `SandboxConfig`     | 见下文    | 沙箱执行器配置。                                                      |
| `policy`        | `PolicyConfig`      | 见下文    | 安全策略引擎配置。                                                    |
| `telemetry`     | `TelemetryConfig`   | 见下文    | 可观测性（日志、审计、指标）配置。                                    |
| `multimodal`    | `MultimodalConfig`  | 见下文    | **后续版本预留** 多模态功能配置，当前版本默认关闭。                    |
| `interface`     | `InterfaceConfig`   | 见下文    | 表现层（CLI / Web）配置。                                             |

---

## 模型管理层 `ModelConfig`

| 字段名                     | 类型                                            | 默认值       | 描述                                                                 |
|----------------------------|-------------------------------------------------|--------------|----------------------------------------------------------------------|
| `providers`                | `Dict[str, ModelProviderConfig]`                | **必填**     | 模型提供商字典，key 为标识符（如 `"default"`、`"claude"`）。         |
| `router_strategy`          | `"priority"` / `"cost"` / `"random"`            | `"priority"` | 模型路由策略：按优先级、成本或随机选择。                             |
| `fallback_chain`           | `List[str]`                                     | `[]`         | 降级顺序，按 provider 标识依次尝试，失败后切换。                    |
| `retry_max_attempts`       | `int`                                           | `3`          | 最大重试次数（包含首次调用），必须 ≥1。                              |
| `retry_backoff_factor`     | `float`                                         | `1.0`        | 指数退避基数（秒），如 1.0 表示重试间隔为 1,2,4,8... 秒。           |

### `ModelProviderConfig`

| 字段名               | 类型                                     | 默认值    | 描述                                                                 |
|----------------------|------------------------------------------|-----------|----------------------------------------------------------------------|
| `provider`           | `"openai"` / `"anthropic"` / `"ollama"` / `"deepseek"` / `"qwen"` | **必填**  | 模型提供商类型。                                                     |
| `api_key`            | `SecretStr` 或 `None`                    | `None`    | API 密钥，支持从环境变量读取（如 `$OPENAI_API_KEY`）。              |
| `base_url`           | `str` 或 `None`                          | `None`    | 自定义 API 端点，用于代理或兼容服务（如 Azure）。                   |
| `default_model`      | `str`                                    | **必填**  | 该提供商默认使用的模型名称（如 `"gpt-4"`）。                        |
| `supports_vision`    | `bool`                                   | `False`   | **预留** 是否支持视觉多模态，后续版本使用。                           |
| `cost_per_1k_input`  | `float`                                  | `0.0`     | 每 1000 输入 token 的成本（美元），用于成本路由和熔断。             |
| `cost_per_1k_output` | `float`                                  | `0.0`     | 每 1000 输出 token 的成本（美元）。                                  |
| `context_window`     | `int`                                    | `1048576` | 上下文窗口大小（token），用于自动压缩阈值计算。                     |
| `max_tokens`         | `int` 或 `None`                          | `None`    | 最大输出 token 数（None=使用客户端默认值 16384）。DeepSeek 等服务商建议 65536~131072。 |

---

## 上下文管理 `ContextConfig`

| 字段名                        | 类型                            | 默认值      | 描述                                                                 |
|-------------------------------|---------------------------------|-------------|----------------------------------------------------------------------|
| `max_tokens`                  | `int`                           | `128000`    | 上下文窗口最大 token 数（含系统、记忆、历史、工具输出）。           |
| `compression_trigger_ratio`   | `float` (0.5~1.0)               | `0.8`       | 触发压缩的 token 使用比例，如 0.8 表示超过 80% 时压缩。             |
| `memory_max_lines`            | `int`                           | `200`       | `MEMORY.md` 最大行数，超过后自动剪枝（保留最新）。                  |
| `keep_system_prompt_first`    | `bool`                          | `True`      | 系统提示是否始终位于消息列表最前（不被压缩顺序打乱）。              |
| `enable_summarization`        | `bool`                          | `True`      | 是否启用 LLM 摘要压缩（否则仅修剪工具输出）。                        |
| `compression_round_interval`  | `int` (≥0)                      | `30`        | 每 N 轮对话强制压缩一次（force=True），0 禁用。与阈值压缩共享计数器。 |
| `multimodal_compression`      | `"drop"` / `"describe"` / `"keep"` | `"keep"`  | **预留** 压缩时对多模态内容的处理方式：丢弃、转为文本描述、保留。  |

---

## 记忆存储 `MemoryConfig`

| 字段名                        | 类型          | 默认值                                      | 描述                                                              |
|-------------------------------|---------------|---------------------------------------------|-------------------------------------------------------------------|
| `project_dir`                 | `Path`        | `./.chacha/memory`                    | 记忆根目录（相对于项目根）。                                     |
| `auto_clean_interval_hours`   | `int`         | `24`                                        | Auto Dream 后台自动清理（合并、剪枝）的执行间隔（小时）。        |
| `max_topic_files`             | `int`         | `10`                                        | 最多保留的主题文件（`topics/*.md`）数量，超出按 LRU 清理。      |

---

## 沙箱执行器 `SandboxConfig`

| 字段名                | 类型            | 默认值（列表）                                                                   | 描述                                                                 |
|-----------------------|-----------------|----------------------------------------------------------------------------------|----------------------------------------------------------------------|
| `allowed_commands`    | `List[str]`     | `["ls","cat","grep","python","pytest","git","echo","head","tail"]`              | 允许执行的命令前缀白名单（子串匹配，建议精确）。                    |
| `timeout_seconds`     | `int` (1~3600)  | `60`                                                                             | 每个命令的超时秒数。                                                 |
| `max_output_lines`    | `int`           | `1000`                                                                           | 命令输出最大行数，超出则截断（防止内存溢出）。                      |
| `working_dir`         | `Path` 或 `None`| `None`                                                                           | 沙箱工作目录，默认使用项目根目录。                                   |

---

## 安全策略 `PolicyConfig`

| 字段名                        | 类型            | 默认值（列表）                                                              | 描述                                                                 |
|-------------------------------|-----------------|-----------------------------------------------------------------------------|----------------------------------------------------------------------|
| `command_blacklist`           | `List[str]`     | `["rm -rf","sudo","chmod 777","dd","mkfs"]`                                | 禁止匹配的命令关键字（子串匹配）。                                    |
| `cost_limit_dollars`          | `float` (≥0)    | `10.0`                                                                      | 单次会话累计成本上限（美元），`0` 表示不限制。                       |
| `approval_cache_ttl_seconds`  | `int` (≥0)      | `300`                                                                       | 工具审批结果缓存时间（秒），`0` 表示不缓存（每次都询问）。          |

---

## 可观测性 `TelemetryConfig`

| 字段名                | 类型                                    | 默认值                                     | 描述                                                              |
|-----------------------|-----------------------------------------|--------------------------------------------|-------------------------------------------------------------------|
| `log_level`           | `"DEBUG"`/`"INFO"`/`"WARNING"`/`"ERROR"`| `"INFO"`                                   | 日志级别。                                                       |
| `enable_audit`        | `bool`                                  | `True`                                     | 是否写入审计日志（`audit.jsonl`）。                               |
| `enable_prometheus`   | `bool`                                  | `False`                                    | 是否暴露 Prometheus `/metrics` 端点。                            |
| `prometheus_port`     | `int` (1~65535)                         | `9090`                                     | Prometheus 监听端口。                                             |
| `audit_log_path`      | `Path`                                  | `./.chacha/logs/audit.jsonl`         | 审计日志文件路径。                                               |
| `debug_log_path`      | `Path`                                  | `./.chacha/logs/debug.jsonl`         | 调试日志文件路径。                                               |

---

## 多模态预留 `MultimodalConfig` (后续版本)

| 字段名                   | 类型                | 默认值    | 描述                                                                 |
|--------------------------|---------------------|-----------|----------------------------------------------------------------------|
| `enabled`                | `bool`              | `False`   | 是否启用多模态功能（当前版本强制为 `false`）。                      |
| `vision_model`           | `str` 或 `None`     | `None`    | 指定视觉模型名称，若为空则自动选择 `supports_vision=True` 的首个提供商。 |
| `max_image_size_mb`      | `int` (≥1)          | `10`      | 单张图片最大大小（MB），超限拒绝或压缩。                            |
| `enable_ocr_fallback`    | `bool`              | `True`    | 图片解析失败时是否降级为 OCR 文本提取。                             |

---

## 表现层 `InterfaceConfig`

| 字段名                     | 类型                                     | 默认值       | 描述                                                                 |
|----------------------------|------------------------------------------|--------------|----------------------------------------------------------------------|
| `cli_theme`                | `"dark"` / `"light"` / `"default"`       | `"default"`  | CLI 配色主题。                                                       |
| `cli_enable_ansi_parser`   | `bool`                                   | `True`       | 是否在 CLI 中解析并渲染 ANSI 转义序列（如颜色）。                   |
| `web_enabled`              | `bool`                                   | `False`      | 是否启动 Web 服务器。                                                |
| `web_host`                 | `str`                                    | `"127.0.0.1"`| Web 服务监听地址。                                                   |
| `web_port`                 | `int` (1~65535)                          | `8080`       | Web 服务端口。                                                       |
| `web_auth_required`        | `bool`                                   | `False`      | 是否启用多用户认证（需要额外配置用户数据库）。                       |

---

## 校验规则说明

- `project_id`：禁止包含 `/`、`\`、`:`、`?` 等路径特殊字符。
- `providers`：必须至少配置一个提供商，否则校验失败。
- 所有枚举字段（如 `router_strategy`、`cli_theme`）只能使用允许的值。
- 数值范围约束（如 `timeout_seconds`、`compression_trigger_ratio`）已在字段定义中标注。
- 额外未知字段将被拒绝（`extra="forbid"`），确保配置严格性。

---

以上所有配置均可通过 `core/config_manager.py` 加载并校验，默认值保证开箱即用。