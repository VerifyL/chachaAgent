"""
core/models/config.py
配置数据模型定义，与 chachaConfig.toml 严格对应。
使用 Pydantic v2 进行类型校验与解析，为 v1.5+ 多模态预留扩展字段。
"""

from pathlib import Path
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field, SecretStr, field_validator


# ==========================
# 模型管理层配置
# ==========================
class ModelProviderConfig(BaseModel):
    """单个模型提供商配置"""
    provider: Literal["openai", "anthropic", "ollama"] = Field(..., description="模型提供商类型")
    api_key: Optional[SecretStr] = Field(None, description="API 密钥，支持从环境变量读取")
    base_url: Optional[str] = Field(None, description="自定义 API 端点，用于代理或兼容服务")
    default_model: str = Field(..., description="默认使用的模型名称")
    supports_vision: bool = Field(False, description="【预留】是否支持视觉多模态，v1.5+ 启用")
    cost_per_1k_input: float = Field(0.0, ge=0, description="每千输入 token 成本（美元）")
    cost_per_1k_output: float = Field(0.0, ge=0, description="每千输出 token 成本（美元）")
    context_window: int = Field(1_048_576, description="上下文窗口大小（token），用于自动压缩阈值")
    max_tokens: Optional[int] = Field(None, description="最大输出 token 数（None=使用客户端默认值 16384）。DeepSeek 等服务商建议 65536~131072")  # noqa: E501


class ModelConfig(BaseModel):
    """模型管理层总配置"""
    providers: Dict[str, ModelProviderConfig] = Field(..., description="提供商标识 => 配置，如 'default', 'claude'")
    router_strategy: Literal["priority", "cost", "random"] = Field("priority", description="模型路由策略")
    fallback_chain: List[str] = Field(default_factory=list, description="降级顺序（按 provider 标识）")
    retry_max_attempts: int = Field(3, ge=1, description="最大重试次数（含首次）")
    retry_backoff_factor: float = Field(1.0, ge=0.1, description="指数退避基数（秒）")


# ==========================
# 策略与沙箱配置
# ==========================
class PolicyConfig(BaseModel):
    """安全策略引擎配置"""
    command_blacklist: List[str] = Field(
        default_factory=lambda: ["rm -rf", "sudo", "chmod 777", "dd", "mkfs"],
        description="禁止执行的命令关键字（子串匹配）"
    )
    cost_limit_dollars: float = Field(10.0, ge=0, description="单次会话成本上限（美元），0 表示不限制")
    approval_cache_ttl_seconds: int = Field(300, ge=0, description="审批结果缓存时间（秒），0 表示不缓存")


class SandboxConfig(BaseModel):
    """沙箱执行器配置"""
    allowed_commands: List[str] = Field(
        default_factory=lambda: ["ls", "cat", "grep", "python", "pytest", "git", "echo", "head", "tail"],
        description="允许执行的命令白名单（前缀匹配）"
    )
    timeout_seconds: int = Field(60, ge=1, le=3600, description="命令执行超时（秒）")
    max_output_lines: int = Field(1000, ge=1, description="命令输出最大行数，超出则截断")
    working_dir: Optional[Path] = Field(None, description="沙箱工作目录，默认使用项目根目录")


# ==========================
# 记忆与上下文配置
# ==========================
class ContextConfig(BaseModel):
    """上下文管理配置"""
    max_tokens: int = Field(1_048_576, ge=1, description="上下文窗口总 token 上限")
    compression_trigger_ratio: float = Field(0.7, ge=0.5, le=1.0, description="触发压缩的 token 使用比例 (0-1)")
    warn_ratio: float = Field(0.9, ge=0.5, le=1.0, description="触发告警的 token 使用比例 (0-1)")
    max_keep_tool_results: int = Field(20, ge=8, description="保留完整工具结果的上限（硬保底 8）")
    memory_max_lines: int = Field(200, ge=1, description="MEMORY.md 最大行数，超出自动剪枝")
    enable_memory_injection: bool = Field(True, description="是否在上下文组装时注入 MEMORY.md 索引（UI 可开关）")
    keep_system_prompt_first: bool = Field(True, description="系统提示是否始终位于消息列表最前")
    enable_summarization: bool = Field(True, description="是否启用 LLM 摘要压缩")

    # 压缩参数
    trim_keep_head: int = Field(5, ge=1, description="TRIMMED: 保留前 N 条消息")
    trim_keep_tail: int = Field(12, ge=1, description="TRIMMED: 保留后 N 条消息")
    summarize_keep_head: int = Field(3, ge=1, description="SUMMARIZED: 保留前 N 条消息")
    summarize_keep_tail: int = Field(8, ge=1, description="SUMMARIZED: 保留后 N 条消息")

    # 多模态压缩策略预留（当前透传）
    multimodal_compression: Literal["drop", "describe", "keep"] = Field("keep", description="【预留】压缩时对多模态内容的处理方式")  # noqa: E501


# ==========================
# 记忆存储配置
# ==========================
class MemoryConfig(BaseModel):
    """记忆子系统（.chacha/memory）配置"""
    project_dir: Path = Field(Path.cwd() / ".chacha" / "memory", description="记忆根目录")
    auto_clean_interval_hours: int = Field(24, ge=1, description="Auto Dream 自动清理间隔（小时）")
    max_topic_files: int = Field(10, ge=1, description="最多保留的主题文件数，超出按 LRU 清理")


# ==========================
# 多模态预留配置（v1.5+）
# TODO(v1.5): 当 VisionClient 实现后，MultimodalConfig 将被 PolicyEngine 和 ModelRouter 消费
# TODO(v1.5): supports_vision 字段需要在 ModelRouter 路由决策中生效
# ==========================
class MultimodalConfig(BaseModel):
    """多模态扩展配置（当前版本仅占位，v1.5+ 启用）"""
    enabled: bool = Field(False, description="是否启用多模态功能")
    vision_model: Optional[str] = Field(None, description="指定视觉模型名称，若为空则自动选择 supports_vision=True 的第一个提供商")  # noqa: E501
    max_image_size_mb: int = Field(10, ge=1, description="单张图片最大大小（MB）")
    enable_ocr_fallback: bool = Field(True, description="图片解析失败时是否降级为 OCR 文本提取")


# ==========================
# 可观测性配置
# ==========================
class TelemetryConfig(BaseModel):
    """统一可观测性配置"""
    enabled: bool = Field(False, description="总开关，关闭则不产生任何日志/指标")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field("INFO", description="日志级别")
    log_dir: Path = Field(Path.home() / ".chacha" / "logs", description="日志目录")
    enable_audit: bool = Field(True, description="是否写入审计日志")
    enable_prometheus: bool = Field(False, description="是否暴露 Prometheus /metrics 端点")
    prometheus_port: int = Field(9090, ge=1, le=65535, description="Prometheus 端口")


# ==========================
# MCP 客户端配置
# ==========================

class MCPServerConfig(BaseModel):
    """单个 MCP server 配置"""
    name: str = Field("", description="服务标识名，如 'filesystem'")
    command: str = Field("", description="启动命令，如 'npx' 或 'python'（SSE 模式可不填）")
    args: List[str] = Field(default_factory=list, description="启动参数列表")
    env: Dict[str, str] = Field(default_factory=dict, description="环境变量")
    transport: Literal["stdio", "sse", "streamable-http"] = Field("stdio", description="传输方式")
    url: Optional[str] = Field(None, description="SSE 端点 URL（transport=sse 时必填）")
    timeout: int = Field(30, ge=1, description="工具调用超时（秒）")
    include: Optional[List[str]] = Field(None, description="白名单：只注入这些工具（不填=全量）")
    exclude: Optional[List[str]] = Field(None, description="黑名单：排除这些工具（不填=全量）")


class MCPConfig(BaseModel):
    """MCP 客户端总配置"""
    enabled: bool = Field(True, description="是否启用 MCP 客户端")
    servers: Dict[str, MCPServerConfig] = Field(
        default_factory=dict, description="server 标识 → 配置"
    )


# ==========================
# 表现层配置
# ==========================
class InterfaceConfig(BaseModel):
    """表现层配置（CLI / Web）"""
    cli_theme: Literal["dark", "light", "default"] = Field("default", description="CLI 配色主题")
    cli_enable_ansi_parser: bool = Field(True, description="是否渲染 ANSI 转义序列")
    web_enabled: bool = Field(False, description="是否启用 Web 服务器")
    web_host: str = Field("127.0.0.1", description="Web 监听地址")
    web_port: int = Field(8080, ge=1, le=65535, description="Web 端口")
    web_auth_required: bool = Field(False, description="是否启用多用户认证")


# ==========================
# 顶层配置聚合
# ==========================
class ChaChaConfig(BaseModel):
    """
    完整的 ChachaAgent 配置，对应 chachaConfig.toml 文件。
    所有子配置均有默认值，未填项使用默认。
    """
    project_id: Optional[str] = Field(None, description="项目标识符，用于隔离会话和记忆，若为空则自动生成")
    environment: Literal["dev", "prod"] = Field("dev", description="运行环境")

    model: ModelConfig
    context: ContextConfig = Field(default_factory=ContextConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)
    multimodal: MultimodalConfig = Field(default_factory=MultimodalConfig)
    mcp: MCPConfig = Field(default_factory=MCPConfig)
    interface: InterfaceConfig = Field(default_factory=InterfaceConfig)

    # ----- 全局校验 -----
    @field_validator("project_id")
    @classmethod
    def validate_project_id(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            # 禁止包含路径分隔符等特殊字符
            if any(c in v for c in "/\\:?"):
                raise ValueError("project_id 不能包含路径特殊字符 ( / \\ : ? )")
        return v

    @field_validator("model")
    @classmethod
    def validate_model_providers(cls, model_cfg: ModelConfig) -> ModelConfig:
        # 至少有一个 provider
        if not model_cfg.providers:
            raise ValueError("必须至少配置一个模型提供商")
        # 如果 router_strategy 为 priority，但 fallback_chain 为空，则自动从 providers 中取第一个作为默认
        if model_cfg.router_strategy == "priority" and not model_cfg.fallback_chain:
            # 此处不自动填充，由上层 ConfigManager 处理，但可发出警告（此处无法）
            pass
        return model_cfg

    model_config = {
        "validate_assignment": True,
        "str_strip_whitespace": True,
        "extra": "forbid",  # 禁止未知字段，保证配置严格性
    }
