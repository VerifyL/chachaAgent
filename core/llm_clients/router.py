"""
core/model/router.py
ModelRouter — 多模型选择与故障转移。

用法:
    router = ModelRouter(config)
    client = router.select(factory)
    # 故障时
    router.mark_failure("default")
    client = router.select(factory)  # → 自动切 fallback
"""

import logging
import random
import time
from typing import Dict, Optional

from core.models.config import ModelConfig

logger = logging.getLogger(__name__)

BAN_TTL_SECONDS = 60.0  # 临时禁用时长


class ModelRouter:
    """多模型选择器"""

    def __init__(self, config: ModelConfig):
        self._providers = config.providers
        self._strategy = config.router_strategy
        self._fallback_chain = config.fallback_chain
        # provider_name → 解禁时间戳
        self._banned_until: Dict[str, float] = {}
        self._failed_count: Dict[str, int] = {}

    # ====== 选择 ======

    def select(self, factory) -> Optional[object]:
        """根据策略选择可用的模型客户端。返回 None 表示全部不可用。"""
        if self._strategy == "priority":
            return self._select_priority(factory)
        if self._strategy == "cost":
            return self._select_cost(factory)
        if self._strategy == "random":
            return self._select_random(factory)
        return self._select_priority(factory)

    def _select_priority(self, factory) -> Optional[object]:
        """按 fallback_chain 顺序，返回第一个可用的"""
        chain = self._fallback_chain or list(self._providers.keys())
        return self._first_available(chain, factory)

    def _select_cost(self, factory) -> Optional[object]:
        """按 cost_per_1k_input 升序，选最便宜的可用"""
        sorted_providers = sorted(
            self._providers.items(),
            key=lambda kv: kv[1].cost_per_1k_input,
        )
        chain = [name for name, _ in sorted_providers]
        return self._first_available(chain, factory)

    def _select_random(self, factory) -> Optional[object]:
        """随机选一个可用"""
        available = [n for n, _ in self._providers.items() if self._is_available(n)]
        if not available:
            return None
        name = random.choice(available)
        return factory.create(self._providers[name])

    def _first_available(self, chain: list, factory) -> Optional[object]:
        for name in chain:
            if self._is_available(name) and name in self._providers:
                client = factory.create(self._providers[name])
                if client:
                    return client
        return None

    # ====== 故障标记 ======

    def mark_failure(self, provider_name: str) -> None:
        """标记 provider 故障，临时禁用 BAN_TTL 秒"""
        self._banned_until[provider_name] = time.time() + BAN_TTL_SECONDS
        self._failed_count[provider_name] = self._failed_count.get(provider_name, 0) + 1
        logger.warning("ModelRouter: %s 已禁用 (%d次失败)", provider_name, self._failed_count[provider_name])

    def mark_success(self, provider_name: str) -> None:
        """标记 provider 恢复正常"""
        self._banned_until.pop(provider_name, None)
        self._failed_count[provider_name] = 0

    # ====== 查询 ======

    def _is_available(self, name: str) -> bool:
        until = self._banned_until.get(name, 0)
        if until > time.time():
            return False
        # 过期自动恢复
        if until > 0 and until <= time.time():
            self._banned_until.pop(name, None)
        return True

    def is_available(self, name: str) -> bool:
        return self._is_available(name)

    def failed_count(self, name: str) -> int:
        return self._failed_count.get(name, 0)
