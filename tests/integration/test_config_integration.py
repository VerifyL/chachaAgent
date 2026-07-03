"""
tests/integration/test_config_integration.py
集成测试：从示例 TOML 文件加载完整配置，验证配置管理器在实际文件场景下的行为。
"""

import os
import pytest
from pathlib import Path

from core.config_manager import ConfigManager
from core.models.config import ChaChaConfig


@pytest.fixture
def full_config_toml(tmp_path: Path) -> Path:
    """创建一个与真实示例一致的完整配置文件"""
    content = """
project_id = "integration-test-project"
environment = "prod"

[model]
router_strategy = "cost"
fallback_chain = ["default", "claude"]
retry_max_attempts = 5
retry_backoff_factor = 2.0

[model.providers.default]
provider = "openai"
api_key = "sk-test123"
base_url = "https://api.openai.com/v1"
default_model = "gpt-4"
supports_vision = false
cost_per_1k_input = 0.01
cost_per_1k_output = 0.03

[model.providers.claude]
provider = "anthropic"
api_key = "sk-ant-test"
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
project_dir = "/custom/memory/path"
auto_clean_interval_hours = 12
max_topic_files = 20

[sandbox]
allowed_commands = ["ls", "pwd"]
timeout_seconds = 30
max_output_lines = 500
working_dir = "/sandbox"

[policy]
command_blacklist = ["rm", "sudo"]
cost_limit_dollars = 5.0
approval_cache_ttl_seconds = 600

[telemetry]
log_level = "DEBUG"
enable_audit = true
enable_prometheus = true
prometheus_port = 9091
log_dir = "logs"

[multimodal]
enabled = true
vision_model = "gpt-4-vision"
max_image_size_mb = 20
enable_ocr_fallback = false

[interface]
cli_theme = "dark"
cli_enable_ansi_parser = false
web_enabled = true
web_host = "0.0.0.0"
web_port = 8081
web_auth_required = true
"""
    file_path = tmp_path / "chachaConfig.toml"
    file_path.write_text(content)
    return file_path


def test_load_full_config_from_file(full_config_toml: Path):
    """从示例 TOML 文件加载完整配置，验证所有字段均被正确解析"""
    mgr = ConfigManager(config_path=full_config_toml)
    config = mgr.load()

    assert isinstance(config, ChaChaConfig)

    # 顶层字段
    assert config.project_id == "integration-test-project"
    assert config.environment == "prod"

    # 模型层
    assert "default" in config.model.providers
    assert "claude" in config.model.providers
    assert config.model.providers["default"].provider == "openai"
    assert config.model.providers["default"].default_model == "gpt-4"
    assert config.model.providers["default"].supports_vision is False
    assert config.model.providers["claude"].supports_vision is True
    assert config.model.router_strategy == "cost"
    assert config.model.fallback_chain == ["default", "claude"]
    assert config.model.retry_max_attempts == 5
    assert config.model.retry_backoff_factor == 2.0

    # 上下文
    assert config.context.max_tokens == 200000
    assert config.context.compression_trigger_ratio == 0.85
    assert config.context.memory_max_lines == 150
    assert config.context.keep_system_prompt_first is False
    assert config.context.enable_summarization is True
    assert config.context.multimodal_compression == "describe"

    # 记忆
    assert config.memory.project_dir == Path("/custom/memory/path")
    assert config.memory.auto_clean_interval_hours == 12
    assert config.memory.max_topic_files == 20

    # 沙箱
    assert config.sandbox.allowed_commands == ["ls", "pwd"]
    assert config.sandbox.timeout_seconds == 30
    assert config.sandbox.max_output_lines == 500
    assert config.sandbox.working_dir == Path("/sandbox")

    # 安全策略
    assert config.policy.command_blacklist == ["rm", "sudo"]
    assert config.policy.cost_limit_dollars == 5.0
    assert config.policy.approval_cache_ttl_seconds == 600

    # 可观测性
    assert config.telemetry.log_level == "DEBUG"
    assert config.telemetry.enable_audit is True
    assert config.telemetry.enable_prometheus is True
    assert config.telemetry.prometheus_port == 9091
    assert config.telemetry.log_dir == Path("logs")

    # 多模态预留
    assert config.multimodal.enabled is True
    assert config.multimodal.vision_model == "gpt-4-vision"
    assert config.multimodal.max_image_size_mb == 20
    assert config.multimodal.enable_ocr_fallback is False

    # 表现层
    assert config.interface.cli_theme == "dark"
    assert config.interface.cli_enable_ansi_parser is False
    assert config.interface.web_enabled is True
    assert config.interface.web_host == "0.0.0.0"
    assert config.interface.web_port == 8081
    assert config.interface.web_auth_required is True


def test_load_config_with_environment_overrides(full_config_toml: Path, monkeypatch):
    """集成测试：环境变量覆盖部分字段后加载"""
    monkeypatch.setenv("CHA_CHA_PROJECT_ID", "env-override-project")
    monkeypatch.setenv("CHA_CHA_MODEL__RETRY_MAX_ATTEMPTS", "10")
    monkeypatch.setenv("CHA_CHA_MULTIMODAL__ENABLED", "false")

    mgr = ConfigManager(config_path=full_config_toml)
    config = mgr.load()

    assert config.project_id == "env-override-project"
    assert config.model.retry_max_attempts == 10
    assert config.multimodal.enabled is False

    # 其他字段应保持不变
    assert config.environment == "prod"
    assert config.model.providers["default"].default_model == "gpt-4"