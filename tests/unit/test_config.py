"""
tests/unit/test_config.py
单元测试：core/models/config.py 中的配置模型
覆盖复杂嵌套配置解析、校验器、默认值、边界条件及多模态预留。
"""

import pytest
from pydantic import ValidationError
from pathlib import Path
from typing import Dict, Any

from core.models.config import (
    ChaChaConfig,
    ModelConfig,
    ModelProviderConfig,
    ContextConfig,
    MemoryConfig,
    SandboxConfig,
    PolicyConfig,
    TelemetryConfig,
    MultimodalConfig,
    InterfaceConfig,
)


# ========== Fixtures ==========
@pytest.fixture
def minimal_config_dict() -> Dict[str, Any]:
    """最小有效配置（仅提供必须字段）"""
    return {
        "model": {
            "providers": {
                "default": {
                    "provider": "openai",
                    "default_model": "gpt-4",
                }
            }
        }
    }


@pytest.fixture
def full_config_dict() -> Dict[str, Any]:
    """完整配置（覆盖所有字段）"""
    return {
        "project_id": "my-project",
        "environment": "prod",
        "model": {
            "providers": {
                "default": {
                    "provider": "openai",
                    "api_key": "sk-12345",
                    "base_url": "https://api.openai.com/v1",
                    "default_model": "gpt-4",
                    "supports_vision": False,
                    "cost_per_1k_input": 0.01,
                    "cost_per_1k_output": 0.03,
                },
                "claude": {
                    "provider": "anthropic",
                    "api_key": "sk-ant-xxx",
                    "default_model": "claude-3-opus",
                    "supports_vision": True,
                    "cost_per_1k_input": 0.015,
                    "cost_per_1k_output": 0.075,
                }
            },
            "router_strategy": "cost",
            "fallback_chain": ["default", "claude"],
            "retry_max_attempts": 5,
            "retry_backoff_factor": 2.0,
        },
        "context": {
            "max_tokens": 200000,
            "compression_trigger_ratio": 0.85,
            "memory_max_lines": 150,
            "keep_system_prompt_first": False,
            "enable_summarization": True,
            "multimodal_compression": "describe",
        },
        "memory": {
            "project_dir": "/custom/memory/path",
            "auto_clean_interval_hours": 12,
            "max_topic_files": 20,
        },
        "sandbox": {
            "allowed_commands": ["ls", "pwd"],
            "timeout_seconds": 30,
            "max_output_lines": 500,
            "working_dir": "/sandbox",
        },
        "policy": {
            "command_blacklist": ["rm", "sudo"],
            "cost_limit_dollars": 5.0,
            "approval_cache_ttl_seconds": 600,
        },
        "telemetry": {
            "log_level": "DEBUG",
            "enable_audit": True,
            "enable_prometheus": True,
            "prometheus_port": 9091,
            "log_dir": "logs",
        },
        "multimodal": {
            "enabled": True,
            "vision_model": "gpt-4-vision",
            "max_image_size_mb": 20,
            "enable_ocr_fallback": False,
        },
        "interface": {
            "cli_theme": "dark",
            "cli_enable_ansi_parser": False,
            "web_enabled": True,
            "web_host": "0.0.0.0",
            "web_port": 8081,
            "web_auth_required": True,
        }
    }


# ========== 1. 基础解析与默认值 ==========
def test_minimal_config(minimal_config_dict):
    """测试仅提供最少字段时的默认值填充"""
    config = ChaChaConfig.model_validate(minimal_config_dict)
    assert config.project_id is None
    assert config.environment == "dev"
    assert config.model.router_strategy == "priority"
    assert config.model.fallback_chain == []
    assert config.model.retry_max_attempts == 3
    assert config.context.max_tokens == 1_048_576
    assert config.context.compression_trigger_ratio == 0.7
    assert config.multimodal.enabled is False
    assert config.multimodal.vision_model is None
    assert config.sandbox.timeout_seconds == 60
    assert config.policy.cost_limit_dollars == 10.0
    assert config.telemetry.log_level == "INFO"
    assert config.interface.web_enabled is False


def test_full_config_parsing(full_config_dict):
    """测试完整配置的正确解析与类型转换"""
    config = ChaChaConfig.model_validate(full_config_dict)
    
    # 顶层
    assert config.project_id == "my-project"
    assert config.environment == "prod"
    
    # 模型
    assert "default" in config.model.providers
    assert config.model.providers["default"].provider == "openai"
    assert config.model.providers["default"].supports_vision is False
    assert config.model.providers["claude"].supports_vision is True
    assert config.model.router_strategy == "cost"
    assert config.model.fallback_chain == ["default", "claude"]
    assert config.model.retry_max_attempts == 5
    assert config.model.retry_backoff_factor == 2.0
    
    # 上下文
    assert config.context.max_tokens == 200000
    assert config.context.multimodal_compression == "describe"
    
    # 记忆
    assert config.memory.project_dir == Path("/custom/memory/path")
    assert config.memory.auto_clean_interval_hours == 12
    
    # 沙箱
    assert config.sandbox.allowed_commands == ["ls", "pwd"]
    assert config.sandbox.timeout_seconds == 30
    assert config.sandbox.working_dir == Path("/sandbox")
    
    # 策略
    assert config.policy.command_blacklist == ["rm", "sudo"]
    assert config.policy.cost_limit_dollars == 5.0
    
    # 可观测
    assert config.telemetry.log_level == "DEBUG"
    assert config.telemetry.enable_prometheus is True
    assert config.telemetry.prometheus_port == 9091
    
    # 多模态
    assert config.multimodal.enabled is True
    assert config.multimodal.vision_model == "gpt-4-vision"
    assert config.multimodal.max_image_size_mb == 20
    assert config.multimodal.enable_ocr_fallback is False
    
    # 表现层
    assert config.interface.cli_theme == "dark"
    assert config.interface.web_enabled is True
    assert config.interface.web_host == "0.0.0.0"
    assert config.interface.web_port == 8081


# ========== 2. 校验器测试 ==========
def test_project_id_validation_invalid_characters():
    """测试 project_id 不允许包含路径分隔符等非法字符"""
    with pytest.raises(ValidationError) as exc:
        ChaChaConfig.model_validate({
            "model": {"providers": {"default": {"provider": "openai", "default_model": "gpt-4"}}},
            "project_id": "my/project"
        })
    assert "project_id 不能包含路径特殊字符" in str(exc.value)
    
    with pytest.raises(ValidationError) as exc:
        ChaChaConfig.model_validate({
            "model": {"providers": {"default": {"provider": "openai", "default_model": "gpt-4"}}},
            "project_id": "my\\project"
        })
    assert "project_id 不能包含路径特殊字符" in str(exc.value)


def test_project_id_validation_accepts_valid():
    """测试合法的 project_id 可以通过"""
    config = ChaChaConfig.model_validate({
        "model": {"providers": {"default": {"provider": "openai", "default_model": "gpt-4"}}},
        "project_id": "my-project_123"
    })
    assert config.project_id == "my-project_123"


def test_model_providers_not_empty():
    """测试必须至少配置一个模型提供商"""
    with pytest.raises(ValidationError) as exc:
        ChaChaConfig.model_validate({
            "model": {"providers": {}}
        })
    assert "必须至少配置一个模型提供商" in str(exc.value)


def test_model_router_strategy_enum():
    """测试 router_strategy 只能为允许的枚举值"""
    with pytest.raises(ValidationError):
        ChaChaConfig.model_validate({
            "model": {
                "providers": {"default": {"provider": "openai", "default_model": "gpt-4"}},
                "router_strategy": "invalid"
            }
        })


def test_interface_cli_theme_enum():
    """测试 cli_theme 只能为允许的值"""
    with pytest.raises(ValidationError):
        ChaChaConfig.model_validate({
            "model": {"providers": {"default": {"provider": "openai", "default_model": "gpt-4"}}},
            "interface": {"cli_theme": "blue"}
        })


# ========== 3. 边界值测试 ==========
def test_cost_limit_zero_allowed():
    """cost_limit_dollars 可以为 0（表示不限制）"""
    config = ChaChaConfig.model_validate({
        "model": {"providers": {"default": {"provider": "openai", "default_model": "gpt-4"}}},
        "policy": {"cost_limit_dollars": 0}
    })
    assert config.policy.cost_limit_dollars == 0


def test_cost_limit_negative_not_allowed():
    """cost_limit_dollars 不能为负数"""
    with pytest.raises(ValidationError):
        ChaChaConfig.model_validate({
            "model": {"providers": {"default": {"provider": "openai", "default_model": "gpt-4"}}},
            "policy": {"cost_limit_dollars": -1}
        })


def test_timeout_seconds_boundary():
    """timeout_seconds 必须在 1~3600 之间"""
    with pytest.raises(ValidationError):
        ChaChaConfig.model_validate({
            "model": {"providers": {"default": {"provider": "openai", "default_model": "gpt-4"}}},
            "sandbox": {"timeout_seconds": 0}
        })
    with pytest.raises(ValidationError):
        ChaChaConfig.model_validate({
            "model": {"providers": {"default": {"provider": "openai", "default_model": "gpt-4"}}},
            "sandbox": {"timeout_seconds": 4000}
        })


def test_compression_trigger_ratio_boundary():
    """compression_trigger_ratio 必须在 0.5~1.0 之间"""
    with pytest.raises(ValidationError):
        ChaChaConfig.model_validate({
            "model": {"providers": {"default": {"provider": "openai", "default_model": "gpt-4"}}},
            "context": {"compression_trigger_ratio": 0.4}
        })
    with pytest.raises(ValidationError):
        ChaChaConfig.model_validate({
            "model": {"providers": {"default": {"provider": "openai", "default_model": "gpt-4"}}},
            "context": {"compression_trigger_ratio": 1.1}
        })


# ========== 4. 多模态预留字段测试 ==========
def test_multimodal_defaults():
    """多模态配置默认值为关闭状态"""
    config = ChaChaConfig.model_validate({
        "model": {"providers": {"default": {"provider": "openai", "default_model": "gpt-4"}}}
    })
    assert config.multimodal.enabled is False
    assert config.multimodal.vision_model is None
    assert config.multimodal.max_image_size_mb == 10
    assert config.multimodal.enable_ocr_fallback is True


def test_multimodal_enabled_custom():
    """可以显式启用多模态并设置自定义参数"""
    config = ChaChaConfig.model_validate({
        "model": {"providers": {"default": {"provider": "openai", "default_model": "gpt-4"}}},
        "multimodal": {
            "enabled": True,
            "vision_model": "custom-vision",
            "max_image_size_mb": 50,
            "enable_ocr_fallback": False
        }
    })
    assert config.multimodal.enabled is True
    assert config.multimodal.vision_model == "custom-vision"
    assert config.multimodal.max_image_size_mb == 50
    assert config.multimodal.enable_ocr_fallback is False


# ========== 5. 复杂嵌套场景 ==========
def test_complex_nested_with_missing_optional():
    """测试部分可选字段缺失时，解析仍能通过"""
    config = ChaChaConfig.model_validate({
        "model": {
            "providers": {
                "default": {"provider": "openai", "default_model": "gpt-4"}
            }
        },
        "sandbox": {
            # 未提供 allowed_commands，应使用默认列表
        }
    })
    # 默认的 allowed_commands 应为预定义列表
    assert config.sandbox.allowed_commands == ["ls", "cat", "grep", "python", "pytest", "git", "echo", "head", "tail"]


def test_provider_api_key_as_secret():
    """api_key 应存储为 SecretStr，打印时隐藏"""
    config = ChaChaConfig.model_validate({
        "model": {
            "providers": {
                "default": {
                    "provider": "openai",
                    "default_model": "gpt-4",
                    "api_key": "sk-12345"
                }
            }
        }
    })
    api_key = config.model.providers["default"].api_key
    assert api_key is not None
    assert str(api_key) != "sk-12345"  # 应被隐藏
    assert api_key.get_secret_value() == "sk-12345"


# ========== 6. 禁止未知字段 ==========
def test_extra_fields_forbidden():
    """配置中包含未定义的字段应报错"""
    with pytest.raises(ValidationError) as exc:
        ChaChaConfig.model_validate({
            "model": {"providers": {"default": {"provider": "openai", "default_model": "gpt-4"}}},
            "unknown_field": "value"
        })
    assert "Extra inputs are not permitted" in str(exc.value)


# ========== 7. 类型转换测试 ==========
def test_path_coercion():
    """字符串路径应自动转换为 Path 对象"""
    config = ChaChaConfig.model_validate({
        "model": {"providers": {"default": {"provider": "openai", "default_model": "gpt-4"}}},
        "telemetry": {
            "log_dir": "some/logs",
        }
    })
    assert isinstance(config.telemetry.log_dir, Path)
    assert config.telemetry.log_dir == Path("some/logs")