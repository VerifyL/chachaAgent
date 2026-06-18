"""
core/model/factory.py
ModelFactory — 根据配置创建模型客户端实例。

用法:
    from core.llm_clients.factory import ModelFactory
    client = ModelFactory.create(config.model)
    invoker = LLMInvoker(model_client=client)
"""

import logging

logger = logging.getLogger(__name__)


class ModelFactory:
    """模型客户端工厂"""

    @staticmethod
    def create(provider_cfg):
        """根据 ModelProviderConfig 创建对应的流式客户端。

        参数 provider_cfg 来自 ModelConfig.providers["default"] 等。
        provider_cfg.type 决定客户端类型：
          - "openai" / "deepseek" / "qwen" → OpenAIClient
          - "ollama"                       → OpenAIClient (兼容 API)
          - "anthropic"                    → TODO(阶段3)

        返回实现了 stream(messages, tools) -> AsyncIterator[StreamChunk] 的对象。
        """
        ptype = provider_cfg.provider.lower()
        api_key = provider_cfg.api_key.get_secret_value() if provider_cfg.api_key else None

        if ptype in ("openai",):
            from core.llm_clients.openai_client import OpenAIClient

            return OpenAIClient(
                api_key=api_key,
                model=provider_cfg.default_model,
                base_url=provider_cfg.base_url or None,
            )

        if ptype == "ollama":
            from core.llm_clients.openai_client import OpenAIClient

            base_url = provider_cfg.base_url or "http://localhost:11434/v1"
            return OpenAIClient(
                api_key=api_key or "ollama",
                model=provider_cfg.default_model,
                base_url=base_url,
            )

        if ptype == "anthropic":
            # TODO(阶段3): 实现 core/model/anthropic_client.py
            logger.warning("Anthropic 客户端尚未实现（阶段 3）")
            return None

        logger.error("未知 model provider: %s", provider_cfg.provider)
        return None
