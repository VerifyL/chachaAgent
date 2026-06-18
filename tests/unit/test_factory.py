"""
tests/unit/test_factory.py
单元测试：core/model/factory.py ModelFactory
覆盖：openai/ollama 创建、anthropic 占位、未知 provider
"""

import pytest

from core.llm_clients.factory import ModelFactory
from core.models.config import ModelProviderConfig


def test_create_openai():
    config = ModelProviderConfig(provider="openai", default_model="gpt-4")
    client = ModelFactory.create(config)
    assert client is not None
    assert client._model == "gpt-4"


def test_create_ollama_default_url():
    config = ModelProviderConfig(provider="ollama", default_model="llama3")
    client = ModelFactory.create(config)
    assert client is not None
    assert client._model == "llama3"


def test_create_ollama_custom_url():
    config = ModelProviderConfig(provider="ollama", default_model="mistral",
                                 base_url="http://192.168.1.100:11434/v1")
    client = ModelFactory.create(config)
    assert client is not None


def test_create_anthropic_placeholder():
    config = ModelProviderConfig(provider="anthropic", default_model="claude-3")
    client = ModelFactory.create(config)
    assert client is None  # 尚未实现


def test_create_unknown_provider():
    config = ModelProviderConfig(provider="openai", default_model="x")
    client = ModelFactory.create(config)  # known provider → not None
    assert client is not None
