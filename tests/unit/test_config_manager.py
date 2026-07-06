"""
tests/unit/test_config_manager.py
单元测试：core/config_manager.py 配置管理器
覆盖配置加载、默认值、校验、缺失报错、环境变量覆盖、热加载及多模态预留配置。
"""

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core import config_manager as cm_module
from core.config_manager import ConfigManager, get_config, get_config_manager, load_config
from core.models.config import ChaChaConfig


# ========== Fixtures ==========
@pytest.fixture(autouse=True)
def reset_singleton():
    """每个测试前重置单例状态，确保测试独立"""
    cm_module._config_manager = None
    ConfigManager._instance = None
    yield
    cm_module._config_manager = None
    ConfigManager._instance = None


@pytest.fixture
def minimal_toml(tmp_path: Path) -> Path:
    """生成最小有效配置 TOML 文件（不包含任何可选段）"""
    toml_content = """
[model]
[model.providers.default]
provider = "openai"
default_model = "gpt-4"
"""
    config_file = tmp_path / "chachaConfig.toml"
    config_file.write_text(toml_content)
    return config_file


@pytest.fixture
def full_toml(tmp_path: Path) -> Path:
    """生成完整配置 TOML 文件（包含所有段，包括 [multimodal]）"""
    toml_content = """
project_id = "my-project"
environment = "prod"

[model]
router_strategy = "cost"
fallback_chain = ["default", "claude"]
retry_max_attempts = 5
retry_backoff_factor = 2.0

[model.providers.default]
provider = "openai"
api_key = "sk-12345"
base_url = "https://api.openai.com/v1"
default_model = "gpt-4"
supports_vision = false
cost_per_1k_input = 0.01
cost_per_1k_output = 0.03

[model.providers.claude]
provider = "anthropic"
api_key = "sk-ant-xxx"
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
    config_file = tmp_path / "chachaConfig.toml"
    config_file.write_text(toml_content)
    return config_file


@pytest.fixture
def harness_toml(tmp_path: Path) -> Path:
    """使用旧名称 harness.toml 的配置文件"""
    toml_content = """
[model]
[model.providers.default]
provider = "openai"
default_model = "gpt-3.5-turbo"
"""
    config_file = tmp_path / "harness.toml"
    config_file.write_text(toml_content)
    return config_file


@pytest.fixture
def invalid_toml(tmp_path: Path) -> Path:
    """无效 TOML 格式文件"""
    config_file = tmp_path / "chachaConfig.toml"
    config_file.write_text("this is not valid toml [")
    return config_file


@pytest.fixture
def invalid_schema_toml(tmp_path: Path) -> Path:
    """配置内容符合 TOML 但不符合 Pydantic 模型"""
    toml_content = """
[model]
[model.providers.default]
provider = "unknown_provider"  # 非法值
default_model = "gpt-4"
"""
    config_file = tmp_path / "chachaConfig.toml"
    config_file.write_text(toml_content)
    return config_file


@pytest.fixture
def empty_toml(tmp_path: Path) -> Path:
    """缺少 model.providers 的 TOML"""
    toml_content = "project_id = 'test'"
    config_file = tmp_path / "chachaConfig.toml"
    config_file.write_text(toml_content)
    return config_file


# ========== 1. 基本加载与默认值测试 ==========
def test_load_minimal_config(minimal_toml: Path):
    """加载最小配置，验证默认值"""
    mgr = ConfigManager(config_path=minimal_toml)
    config = mgr.load()
    assert isinstance(config, ChaChaConfig)
    assert config.project_id is None
    assert config.environment == "dev"
    assert config.model.router_strategy == "priority"
    assert config.model.retry_max_attempts == 3
    assert config.context.max_tokens == 1_048_576
    assert config.multimodal.enabled is False
    assert config.policy.cost_limit_dollars == 10.0
    assert config.sandbox.timeout_seconds == 60
    assert config.telemetry.log_level == "INFO"
    assert config.interface.web_enabled is False


def test_load_full_config(full_toml: Path):
    """加载完整配置，验证所有字段解析正确"""
    mgr = ConfigManager(config_path=full_toml)
    config = mgr.load()
    assert config.project_id == "my-project"
    assert config.environment == "prod"
    assert config.model.providers["default"].provider == "openai"
    assert config.model.providers["claude"].supports_vision is True
    assert config.model.router_strategy == "cost"
    assert config.model.fallback_chain == ["default", "claude"]
    assert config.context.max_tokens == 200000
    assert config.context.multimodal_compression == "describe"
    assert config.memory.project_dir == Path("/custom/memory/path")
    assert config.sandbox.timeout_seconds == 30
    assert config.policy.cost_limit_dollars == 5.0
    assert config.telemetry.log_level == "DEBUG"
    assert config.multimodal.enabled is True
    assert config.interface.web_enabled is True


def test_load_harness_toml(harness_toml: Path, tmp_path: Path):
    """优先查找 chachaConfig.toml，回退到 harness.toml"""
    original_cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        assert not (tmp_path / "chachaConfig.toml").exists()
        mgr = ConfigManager()
        config = mgr.load()
        assert config.model.providers["default"].default_model == "gpt-3.5-turbo"
    finally:
        os.chdir(original_cwd)


def test_load_with_specified_path(full_toml: Path):
    """指定配置文件路径时优先使用"""
    mgr = ConfigManager(config_path=full_toml)
    config = mgr.load()
    assert config.project_id == "my-project"


# ========== 2. 缺失与错误处理 ==========
def test_load_missing_file(tmp_path: Path):
    """配置文件不存在时自动生成默认模板 → 不抛异常"""
    mgr = ConfigManager(config_path=tmp_path / "nonexistent.toml")
    # v2.1: 不存在时自动生成默认配置，不再抛 FileNotFoundError
    config = mgr.load()
    assert isinstance(config, ChaChaConfig)


def test_load_invalid_toml(invalid_toml: Path):
    """TOML 格式错误时抛出 ValueError"""
    mgr = ConfigManager(config_path=invalid_toml)
    with pytest.raises(ValueError, match="TOML 解析失败"):
        mgr.load()


def test_load_invalid_schema(invalid_schema_toml: Path):
    """Pydantic 校验失败时抛出 ValueError"""
    mgr = ConfigManager(config_path=invalid_schema_toml)
    with pytest.raises(ValueError, match="配置文件格式错误"):
        mgr.load()


def test_load_empty_providers(empty_toml: Path):
    """缺少 model.providers 时校验失败"""
    mgr = ConfigManager(config_path=empty_toml)
    with pytest.raises(ValueError, match="配置文件格式错误"):
        mgr.load()


# ========== 3. 环境变量覆盖测试 ==========
def test_env_override_simple(monkeypatch, minimal_toml: Path):
    """环境变量覆盖顶层字段"""
    monkeypatch.setenv("CHA_CHA_PROJECT_ID", "env-project")
    mgr = ConfigManager(config_path=minimal_toml)
    config = mgr.load()
    assert config.project_id == "env-project"


def test_env_override_nested(monkeypatch, minimal_toml: Path):
    """环境变量覆盖嵌套字段（使用 __ 分隔）"""
    monkeypatch.setenv("CHA_CHA_MODEL__PROVIDERS__DEFAULT__API_KEY", "env-api-key")
    mgr = ConfigManager(config_path=minimal_toml)
    config = mgr.load()
    api_key = config.model.providers["default"].api_key
    assert api_key is not None
    assert api_key.get_secret_value() == "env-api-key"


def test_env_override_type_conversion(monkeypatch, minimal_toml: Path):
    """环境变量值自动类型转换（布尔、数字）"""
    monkeypatch.setenv("CHA_CHA_MULTIMODAL__ENABLED", "true")
    monkeypatch.setenv("CHA_CHA_CONTEXT__MAX_TOKENS", "9999")
    mgr = ConfigManager(config_path=minimal_toml)
    config = mgr.load()
    assert config.multimodal.enabled is True
    assert config.context.max_tokens == 9999


def test_env_override_float(monkeypatch, minimal_toml: Path):
    """环境变量浮点数转换"""
    monkeypatch.setenv("CHA_CHA_MODEL__RETRY_BACKOFF_FACTOR", "2.5")
    mgr = ConfigManager(config_path=minimal_toml)
    config = mgr.load()
    assert config.model.retry_backoff_factor == 2.5


def test_env_override_no_matching(monkeypatch, minimal_toml: Path):
    """无关环境变量不影响配置（且不会导致 extra 错误）"""
    monkeypatch.setenv("CHA_CHA_UNKNOWN", "value")
    monkeypatch.setenv("OTHER_VAR", "value")
    mgr = ConfigManager(config_path=minimal_toml)
    config = mgr.load()
    assert config.project_id is None


# ========== 4. 单例与缓存测试 ==========
def test_singleton(minimal_toml: Path):
    """ConfigManager 是单例"""
    mgr1 = ConfigManager(config_path=minimal_toml)
    mgr2 = ConfigManager(config_path=minimal_toml)
    assert mgr1 is mgr2


def test_load_caching(minimal_toml: Path):
    """多次调用 load() 返回缓存对象，除非 force=True"""
    mgr = ConfigManager(config_path=minimal_toml)
    config1 = mgr.load()
    config2 = mgr.load()
    assert config1 is config2

    with patch.object(mgr, "_read_toml", wraps=mgr._read_toml) as mock_read:
        config3 = mgr.load(force=True)
        assert config3 is not config1
        mock_read.assert_called_once()


def test_path_change_clears_cache(minimal_toml: Path, tmp_path: Path):
    """切换路径时自动清除缓存并重新加载"""
    mgr = ConfigManager(config_path=minimal_toml)
    config1 = mgr.load()
    assert config1.project_id is None

    new_toml = tmp_path / "new_config.toml"
    new_toml.write_text("""
project_id = "new-project"
[model]
[model.providers.default]
provider = "openai"
default_model = "gpt-4"
""")
    mgr = ConfigManager(config_path=new_toml)
    config2 = mgr.load()
    assert config2.project_id == "new-project"
    assert config1 is not config2


# ========== 5. 热加载功能（需要 mock watchdog） ==========
def test_start_watch_no_watchdog(minimal_toml: Path):
    """未安装 watchdog 时发出警告但不报错"""
    mgr = ConfigManager(config_path=minimal_toml)
    with patch.dict("sys.modules", {"watchdog": None}):
        mgr.start_watch()
        assert mgr._watcher is None


@pytest.mark.skip(reason="watchdog 未安装，跳过")
def test_start_watch_with_watchdog(minimal_toml: Path):
    """正常启动 watch（需要 mock Observer）"""
    mgr = ConfigManager(config_path=minimal_toml)
    mock_observer = MagicMock()
    with patch("watchdog.observers.Observer", return_value=mock_observer):
        mgr.start_watch()
        assert mgr._watcher is mock_observer
        mock_observer.start.assert_called_once()


def test_stop_watch(minimal_toml: Path):
    """停止监听"""
    mgr = ConfigManager(config_path=minimal_toml)
    mock_observer = MagicMock()
    mgr._watcher = mock_observer
    mgr.stop_watch()
    mock_observer.stop.assert_called_once()
    mock_observer.join.assert_called_once()
    assert mgr._watcher is None


@pytest.mark.skip(reason="watchdog 未安装，跳过")
def test_watch_callback(minimal_toml: Path):
    """注册回调并在文件修改时触发"""
    mgr = ConfigManager(config_path=minimal_toml)
    callback = MagicMock()
    mgr.register_callback(callback)

    with patch("watchdog.observers.Observer") as mock_observer_cls:
        mock_observer = MagicMock()
        mock_observer_cls.return_value = mock_observer
        mgr.start_watch()

        from watchdog.events import FileModifiedEvent

        _event = FileModifiedEvent(str(minimal_toml.resolve()))
        mgr.reload = MagicMock(return_value=mgr._config)
        for cb in mgr._watch_callbacks:
            cb(mgr._config)
        callback.assert_called_once_with(mgr._config)


# ========== 6. 全局便捷函数测试 ==========
def test_global_get_config_manager(minimal_toml: Path):
    """get_config_manager 返回单例"""
    cm_module._config_manager = None
    mgr1 = get_config_manager()
    mgr1._config_path = minimal_toml
    mgr2 = get_config_manager()
    assert mgr1 is mgr2


def test_load_config_and_get_config(minimal_toml: Path):
    """load_config 和 get_config 快捷函数"""
    cm_module._config_manager = None
    mgr = get_config_manager()
    mgr._config_path = minimal_toml
    config = load_config()
    assert isinstance(config, ChaChaConfig)
    config2 = get_config()
    assert config2 is config


# ========== 7. 多模态预留配置专项测试（新增） ==========
def test_multimodal_config_defaults(minimal_toml: Path):
    """验证未配置 [multimodal] 段时的默认值（与 test_load_minimal_config 互补）"""
    mgr = ConfigManager(config_path=minimal_toml)
    config = mgr.load()
    assert config.multimodal.enabled is False
    assert config.multimodal.vision_model is None
    assert config.multimodal.max_image_size_mb == 10
    assert config.multimodal.enable_ocr_fallback is True


def test_multimodal_config_custom(full_toml: Path):
    """验证 [multimodal] 段自定义值正确加载（与 test_load_full_config 互补）"""
    mgr = ConfigManager(config_path=full_toml)
    config = mgr.load()
    assert config.multimodal.enabled is True
    assert config.multimodal.vision_model == "gpt-4-vision"
    assert config.multimodal.max_image_size_mb == 20
    assert config.multimodal.enable_ocr_fallback is False
