"""
tests/unit/test_router.py
单元测试：core/model/router.py ModelRouter
覆盖：priority/cost/random 策略、故障转移、临时禁用恢复
"""

import pytest

from core.llm_clients.router import ModelRouter
from core.models.config import ModelConfig, ModelProviderConfig

# ====== Fixtures ======


@pytest.fixture
def providers():
    return {
        "default": ModelProviderConfig(provider="openai", default_model="gpt-4", cost_per_1k_input=0.003),
        "cheap": ModelProviderConfig(provider="openai", default_model="deepseek", cost_per_1k_input=0.001),
        "local": ModelProviderConfig(provider="ollama", default_model="llama3", cost_per_1k_input=0.0),
    }


@pytest.fixture
def factory(providers):
    class FakeFactory:
        @staticmethod
        def create(cfg):
            return f"client:{cfg.default_model}"

    return FakeFactory()


# ====== 1. priority 策略 ======


def test_priority_with_fallback_chain(providers, factory):
    config = ModelConfig(
        providers=providers,
        router_strategy="priority",
        fallback_chain=["default", "cheap", "local"],
    )
    router = ModelRouter(config)
    client = router.select(factory)
    assert client == "client:gpt-4"


def test_priority_fallback_when_primary_banned(providers, factory):
    config = ModelConfig(
        providers=providers,
        router_strategy="priority",
        fallback_chain=["default", "cheap"],
    )
    router = ModelRouter(config)
    router.mark_failure("default")

    client = router.select(factory)
    assert client == "client:deepseek"


def test_priority_all_banned_returns_none(providers, factory):
    config = ModelConfig(
        providers=providers,
        router_strategy="priority",
        fallback_chain=["default", "cheap"],
    )
    router = ModelRouter(config)
    router.mark_failure("default")
    router.mark_failure("cheap")

    client = router.select(factory)
    assert client is None


def test_priority_recovery_after_ban_expiry(providers, factory):
    import time

    config = ModelConfig(providers=providers, fallback_chain=["default"])

    # 缩短 BAN TTL 以加速测试
    router = ModelRouter(config)
    router._banned_until["default"] = time.time() - 1  # 已过期

    client = router.select(factory)
    assert client == "client:gpt-4"


# ====== 2. cost 策略 ======


def test_cost_picks_cheapest(providers, factory):
    config = ModelConfig(providers=providers, router_strategy="cost")
    router = ModelRouter(config)
    client = router.select(factory)
    assert client == "client:llama3"  # cost=0.0, 最便宜


def test_cost_skips_banned(providers, factory):
    config = ModelConfig(providers=providers, router_strategy="cost")
    router = ModelRouter(config)
    router.mark_failure("local")  # 最便宜的挂了

    client = router.select(factory)
    assert client == "client:deepseek"  # 次便宜


# ====== 3. random 策略 ======


def test_random_returns_client(providers, factory):
    config = ModelConfig(providers=providers, router_strategy="random")
    router = ModelRouter(config)
    client = router.select(factory)
    assert client is not None
    assert client.startswith("client:")


def test_random_all_banned_returns_none(providers, factory):
    config = ModelConfig(providers=providers, router_strategy="random")
    router = ModelRouter(config)
    for name in providers:
        router.mark_failure(name)
    assert router.select(factory) is None


# ====== 4. 故障追踪 ======


def test_failed_count(providers, factory):
    config = ModelConfig(providers=providers)
    router = ModelRouter(config)
    router.mark_failure("default")
    router.mark_failure("default")
    assert router.failed_count("default") == 2
    assert router.failed_count("cheap") == 0


def test_mark_success_resets_count(providers, factory):
    config = ModelConfig(providers=providers)
    router = ModelRouter(config)
    router.mark_failure("default")
    router.mark_success("default")
    assert router.failed_count("default") == 0
    assert router.is_available("default") is True


# ====== 5. 降级策略 ======


def test_verify_supports_vision_field(providers, factory):
    """验证 supports_vision 属性可访问（v1.5 预留）"""
    providers["default"].supports_vision = True
    config = ModelConfig(providers=providers, fallback_chain=["default"])
    router = ModelRouter(config)
    client = router.select(factory)
    assert client is not None
